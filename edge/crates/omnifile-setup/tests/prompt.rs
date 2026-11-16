//! The console conversation, scripted: plain questions, answers checked as
//! they come, the system disk never offered, the password never echoed.

use std::time::Duration;

use omnifile_setup::answers::Answers;
use omnifile_setup::host::Fake;
use omnifile_setup::prompt::{self, Scripted};
use omnifile_setup::render::DATA_MOUNT_UNIT;
use omnifile_setup::steps::{self, Ctx, Outcome};

fn machine() -> Fake {
    let mut h = Fake::default();
    h.dirs
        .insert("/sys/class/net".into(), vec!["ens3".into(), "lo".into()]);
    h.files
        .insert("/sys/class/net/ens3/device".into(), (String::new(), 0o644));
    h.answer(
        "lsblk -dnpro NAME,SIZE,TYPE,MODEL",
        0,
        "/dev/vda 10G disk QEMU\\x20HARDDISK\n/dev/sr0 1G rom DVD\n/dev/vdb 2T disk Data\\x20Disk\n",
    );
    h.answer("lsblk -nro MOUNTPOINTS /dev/vda", 0, "\n/efi\n/\n/usr\n");
    h.answer("lsblk -nro MOUNTPOINTS /dev/vdb", 0, "\n");
    h.answer("lsblk -dno TYPE", 0, "disk\n");
    // The system disk's inactive /usr slot (A/B updates): unformatted, unmounted.
    h.answer("lsblk -dno TYPE /dev/vda4", 0, "part\n");
    h.answer("lsblk -nro MOUNTPOINTS /dev/vda4", 0, "\n");
    h.files.insert("/dev/vda4".into(), (String::new(), 0o660));
    h.answer("net ads testjoin", 1, "");
    h
}

#[test]
fn a_first_setup_conversation() {
    let mut h = machine();
    let mut c = Scripted::new(&[
        "edge_1", // refused: underscore
        "edge1",
        "2",         // fixed address
        "10.1.2.10", // refused: no prefix
        "10.1.2.10/24",
        "10.1.2.1",
        "10.1.0.10 dc2", // refused: not an IP
        "10.1.0.10 10.1.0.11",
        "", // the one free disk is the default
        "corp.example",
        "", // CORP from the domain
        "svc-join",
        "", // default OU
        "filer admins",
        "s3cret-pw", // hidden
        "",          // private endpoints: yes
        "ofe1.code",
        "", // no update feed
        "", // default window
        "", // no files in S3
        "y",
    ]);
    let asked = prompt::questions(&mut c, &mut h, None).unwrap();
    let a = asked.answers;
    assert_eq!(a.hostname, "edge1");
    assert_eq!(a.network.mode, "static");
    assert_eq!(a.network.address, "10.1.2.10/24");
    assert_eq!(a.network.dns, vec!["10.1.0.10", "10.1.0.11"]);
    assert_eq!(a.data.device, "/dev/vdb");
    assert!(!a.data.erase);
    assert_eq!(a.ad.workgroup, "CORP");
    assert_eq!(a.ad.admin_group, "filer admins");
    assert!(a.storage.private_endpoints);
    assert_eq!(a.service.bundle, "ofe1.code");
    assert_eq!(a.updates.reboot_window, "Sun *-*-* 03:00");
    assert_eq!(asked.join_password.as_deref(), Some("s3cret-pw"));
    assert!(a.problems().is_empty());

    let said = &c.said;
    assert!(
        said.contains("/dev/vdb  2T  Data Disk"),
        "the free disk is offered: {said}"
    );
    assert!(
        !said.contains("/dev/vda"),
        "the system disk is never offered"
    );
    assert!(!said.contains("s3cret-pw"), "the password is not echoed");
    assert!(said.contains("starting with a letter"));
    assert!(said.contains("prefix"));
    assert!(said.contains("dc2 is not an IP address"));
    assert!(said.contains("name           edge1 (edge1.corp.example)"));
}

#[test]
fn a_disk_with_data_is_formatted_only_after_erase() {
    let mut h = machine();
    h.answer("wipefs --no-act", 0, "ntfs\n");
    let script = |last: &str| Scripted::new(&["edge1", "1", "", "", last]);
    let mut c = script("no");
    let e = prompt::questions(&mut c, &mut h, None).err().unwrap();
    assert!(e.contains("left as it is"), "{e}");
    assert!(c.said.contains("it holds ntfs"));

    let mut c = Scripted::new(&[
        "edge1",
        "1",
        "",
        "",
        "ERASE",
        "corp.example",
        "",
        "svc",
        "",
        "admins",
        "pw",
        "",
        "",
        "",
        "",
        "",
        "y",
    ]);
    let asked = prompt::questions(&mut c, &mut h, None).unwrap();
    assert!(asked.answers.data.erase);
    assert!(c.said.contains("(WILL BE ERASED)"));
}

#[test]
fn saying_no_at_the_end_changes_nothing() {
    let mut h = machine();
    let mut c = Scripted::new(&[
        "edge1",
        "1",
        "",
        "",
        "corp.example",
        "",
        "svc",
        "",
        "admins",
        "pw",
        "",
        "",
        "",
        "",
        "",
        "n",
    ]);
    let e = prompt::questions(&mut c, &mut h, None).err().unwrap();
    assert!(e.contains("nothing was changed"));
}

#[test]
fn a_rerun_offers_the_previous_answers_and_skips_what_is_done() {
    let mut h = machine();
    h.answer("net ads testjoin", 0, "Join is OK\n");
    h.files.insert(
        omnifile_setup::steps::IDENTITY_JSON.into(),
        (String::new(), 0o644),
    );
    let before = omnifile_setup::answers::Answers::parse(
        r#"
        version = 1
        hostname = "edge7"
        [network]
        mode = "dhcp"
        [data]
        device = "/dev/vdb"
        [ad]
        domain = "corp.example"
        workgroup = "CORP"
        join_user = "svc"
        admin_group = "admins"
        [updates]
        reboot_window = "Sat *-*-* 01:00"
        "#,
    )
    .unwrap();
    // Every answer kept: Enter throughout. No password (joined), no code (enrolled).
    let mut c = Scripted::new(&["", "", "", "", "", "", "", "", "", "", "", "", "", "y"]);
    let asked = prompt::questions(&mut c, &mut h, Some(&before)).unwrap();
    assert_eq!(asked.answers.hostname, "edge7");
    assert_eq!(asked.answers.updates.reboot_window, "Sat *-*-* 01:00");
    assert!(asked.join_password.is_none());
    assert!(!c.said.contains("Password of"));
    assert!(!c.said.contains("enrolment code"));
}

/// The first run's answers: DHCP, the one free disk, no code, no feed, no S3.
const FIRST_RUN: [&str; 16] = [
    "edge1",
    "1",
    "",
    "", // the one free disk
    "corp.example",
    "",
    "svc",
    "",
    "admins",
    "pw",
    "",
    "",
    "",
    "",
    "",
    "y",
];

/// A rerun with every answer kept (Enter throughout), the password given again.
const RERUN: [&str; 16] = [
    "", "", "", "", // keep the data disk
    "", "", "", "", "", "pw", "", "", "", "", "", "y",
];

/// The machine as a first console run leaves it when the domain join fails:
/// the data disk formatted by setup (xfs, labelled omni-data) and mounted at
/// the data root by setup's own mount unit, the answers saved, not joined.
fn after_a_failed_join() -> (Fake, Answers) {
    let mut h = machine();
    h.files.insert("/dev/vdb".into(), (String::new(), 0o660));
    h.answer("blkid -p -o value -s UUID /dev/vdb", 0, "7d2c-uuid\n");
    // The data root mounts once the step has formatted the disk and enabled its unit.
    h.answer(
        "findmnt -n -o FSTYPE,UUID /srv/omnifile/data",
        0,
        "xfs 7d2c-uuid\n",
    );
    h.answer_once("findmnt -n -o FSTYPE,UUID /srv/omnifile/data", 1, "");
    h.answer("net ads join", 1, "Failed to join domain");
    let mut c = Scripted::new(&FIRST_RUN);
    let asked = prompt::questions(&mut c, &mut h, None).unwrap();
    let mut ctx = Ctx::new(asked.answers);
    ctx.join_password = asked.join_password;
    ctx.wait = Duration::ZERO;
    let f = steps::run(&mut h, &mut ctx, None, |_, _| {}).unwrap_err();
    assert_eq!(f.step, "ad", "{f}");
    assert!(h.ran_any("mkfs.xfs -f -L omni-data /dev/vdb"));
    assert!(
        h.file(DATA_MOUNT_UNIT)
            .unwrap()
            .contains("What=/dev/disk/by-uuid/7d2c-uuid")
    );
    // What the box now shows: the volume stays mounted where its unit put it.
    h.answer("lsblk -nro MOUNTPOINTS /dev/vdb", 0, "/srv/omnifile/data\n");
    h.answer("wipefs --no-act", 0, "xfs\n");
    h.answer("blkid -p -o value -s LABEL /dev/vdb", 0, "omni-data\n");
    h.answer("blkid -p -o value -s TYPE /dev/vdb", 0, "xfs\n");
    (h, ctx.answers)
}

#[test]
fn a_rerun_after_a_failed_join_keeps_the_data_disk_and_joins() {
    let (mut h, before) = after_a_failed_join();
    let first = h.ran.len();
    h.answer(
        "net ads join",
        0,
        "Joined 'EDGE1' to dns domain 'corp.example'\n",
    );

    let mut c = Scripted::new(&RERUN);
    let asked = prompt::questions(&mut c, &mut h, Some(&before)).unwrap();
    assert_eq!(asked.answers.data.device, "/dev/vdb");
    assert!(!asked.answers.data.erase);
    assert!(
        c.said.contains("/dev/vdb") && c.said.contains("Keep it"),
        "the disk setup made is offered to keep: {}",
        c.said
    );
    assert!(!c.said.contains("ERASE"), "{}", c.said);
    assert!(!c.said.contains("(WILL BE ERASED)"));

    // The steps go on to the join, and never touch the disk.
    let mut ctx = Ctx::new(asked.answers);
    ctx.join_password = asked.join_password;
    ctx.wait = Duration::ZERO;
    let done = steps::run(&mut h, &mut ctx, None, |_, _| {}).unwrap();
    let data = done.iter().find(|(s, _)| *s == "data").unwrap();
    assert_eq!(data.1, Outcome::Unchanged, "{done:?}");
    let ad = done.iter().find(|(s, _)| *s == "ad").unwrap();
    assert!(
        matches!(&ad.1, Outcome::Changed(w) if w.contains("joined the domain")),
        "{done:?}"
    );
    let again = &h.ran[first..];
    assert!(
        !again
            .iter()
            .any(|l| l.starts_with("mkfs") || l.starts_with("wipefs --all")),
        "{again:?}"
    );
}

#[test]
fn a_rerun_that_declines_the_data_disk_changes_nothing() {
    let (mut h, before) = after_a_failed_join();
    let mut script = RERUN;
    script[3] = "n";
    let mut c = Scripted::new(&script);
    let e = prompt::questions(&mut c, &mut h, Some(&before))
        .err()
        .unwrap();
    assert!(
        e.contains("nothing was changed") && e.contains("/dev/vdb"),
        "{e}"
    );
    assert!(!c.said.contains("ERASE"));
}

#[test]
fn a_mounted_disk_setup_did_not_make_is_never_kept_or_offered() {
    // Mounted at the data root, but not the volume setup's own mount unit
    // names (another UUID), or not labelled omni-data, or also holding the
    // system: in use, so refused, and the question is asked again.
    type Change = fn(&mut Fake);
    let cases: [(&str, Change); 3] = [
        ("another uuid", |h| {
            h.answer("blkid -p -o value -s UUID /dev/vdb", 0, "0000-other\n");
        }),
        ("another label", |h| {
            h.answer("blkid -p -o value -s LABEL /dev/vdb", 0, "projects\n");
        }),
        ("the system too", |h| {
            h.answer(
                "lsblk -nro MOUNTPOINTS /dev/vdb",
                0,
                "/srv/omnifile/data\n/\n",
            );
        }),
    ];
    for (what, change) in cases {
        let (mut h, before) = after_a_failed_join();
        change(&mut h);
        let mut c = Scripted::new(&["", "", "", "/dev/vdb", "/dev/vda"]);
        let e = prompt::questions(&mut c, &mut h, Some(&before))
            .err()
            .unwrap();
        assert!(e.contains("input ended"), "{what}: {e}");
        assert!(!c.said.contains("Keep it"), "{what}: {}", c.said);
        assert!(
            c.said
                .contains("/dev/vdb is in use (mounted at /srv/omnifile/data"),
            "{what}: {}",
            c.said
        );
        assert!(
            c.said
                .contains("/dev/vda is in use (mounted at /efi, /, /usr)"),
            "{what}: the system disk is refused and asked again: {}",
            c.said
        );
    }
}

#[test]
fn the_system_disk_typed_by_hand_is_refused_and_asked_again() {
    let mut h = machine();
    // Aliases name what they point at (review on #37).
    let by_path = "/dev/disk/by-path/virtio-pci-0000:00:04.0";
    // (Answers match by prefix, the latest first: the longer name goes last.)
    h.answer(&format!("realpath -e -- {by_path}"), 0, "/dev/vda\n");
    h.answer(&format!("realpath -e -- {by_path}-part4"), 0, "/dev/vda4\n");
    let part4 = format!("{by_path}-part4");
    let mut c = Scripted::new(&[
        "edge1",
        "1",
        "",
        "/dev/vda",  // refused: it holds the system
        "/dev/vda4", // refused: a partition (the system disk's free /usr slot)
        &part4,      // refused: the same partition, by another name
        by_path,     // refused: the system disk, by another name
        "/dev/nope", // refused: no such device
        "/dev/vdb",
        "corp.example",
        "",
        "svc",
        "",
        "admins",
        "pw",
        "",
        "",
        "",
        "",
        "",
        "y",
    ]);
    h.answer("realpath -e -- /dev/nope", 1, "");
    let a = prompt::questions(&mut c, &mut h, None).unwrap().answers;
    assert_eq!(a.data.device, "/dev/vdb");
    for refusal in [
        "/dev/vda is in use (mounted at /efi, /, /usr): choose an empty disk".to_string(),
        "/dev/vda4 is a partition, not a whole disk".to_string(),
        format!("{part4} (/dev/vda4) is a partition, not a whole disk"),
        format!("{by_path} (/dev/vda) is in use (mounted at /efi, /, /usr)"),
        "there is no disk /dev/nope".to_string(),
    ] {
        assert!(c.said.contains(&refusal), "{refusal}: {}", c.said);
    }
    assert!(!c.said.contains("ERASE"), "{}", c.said);
}

/// The reviewer's machine (review on #37): one disk, the system's, whose
/// inactive /usr slot (GPT label `_empty`, the next update's target) is
/// unformatted, so nothing marks it as holding anything.
fn one_disk_appliance() -> Fake {
    let mut h = Fake::default();
    h.dirs
        .insert("/sys/class/net".into(), vec!["ens3".into(), "lo".into()]);
    h.files
        .insert("/sys/class/net/ens3/device".into(), (String::new(), 0o644));
    h.answer(
        "lsblk -dnpro NAME,SIZE,TYPE,MODEL",
        0,
        "/dev/vda 10G disk QEMU\\x20HARDDISK\n",
    );
    h.answer("lsblk -nro MOUNTPOINTS /dev/vda", 0, "\n/efi\n/\n/usr\n");
    h.answer("lsblk -dno TYPE", 0, "disk\n");
    h.answer("lsblk -dno TYPE /dev/vda4", 0, "part\n");
    h.answer("lsblk -nro MOUNTPOINTS /dev/vda4", 0, "\n");
    h.files.insert("/dev/vda4".into(), (String::new(), 0o660));
    h.answer("blkid -p -o value -s UUID /dev/vda4", 0, "aaaa-uuid\n");
    h.answer("net ads testjoin", 1, "");
    h
}

#[test]
fn a_partition_of_the_system_disk_is_never_the_data_disk() {
    // On main this answered the question, asked no ERASE, and the data step
    // ran `mkfs.xfs -f -L omni-data /dev/vda4`.
    let mut h = one_disk_appliance();
    let mut c = Scripted::new(&["edge1", "1", "", "/dev/vda4"]);
    let e = prompt::questions(&mut c, &mut h, None).err().unwrap();
    assert!(e.contains("input ended"), "{e}");
    assert!(
        c.said
            .contains("/dev/vda4 is a partition, not a whole disk"),
        "{}",
        c.said
    );
    // The same answer from a file or a credential: the data step refuses it.
    let mut a = Answers::parse(
        "version = 1\nhostname = \"edge1\"\n[network]\nmode = \"dhcp\"\n[data]\n\
         device = \"/dev/vda4\"\n[ad]\ndomain = \"corp.example\"\nworkgroup = \"CORP\"\n\
         join_user = \"svc\"\nadmin_group = \"admins\"\n",
    )
    .unwrap();
    a.data.erase = true;
    let mut ctx = Ctx::new(a);
    let f = steps::run(&mut h, &mut ctx, Some(&["data".into()]), |_, _| {}).unwrap_err();
    assert!(f.message.contains("is a partition"), "{f}");
    assert!(
        !h.ran_any("mkfs") && !h.ran_any("wipefs --all"),
        "{:?}",
        h.ran
    );
}

#[test]
fn roles_anywhere_is_asked_with_each_arn_checked() {
    let ta = "arn:aws:rolesanywhere:eu-west-1:123456789012:trust-anchor/1f2e3d4c-5b6a-4978-8a1b-2c3d4e5f6a7b";
    let profile =
        "arn:aws:rolesanywhere:eu-west-1:123456789012:profile/0a1b2c3d-4e5f-4a6b-8c7d-9e0f1a2b3c4d";
    let role = "arn:aws:iam::123456789012:role/omnifile-edge-s3-read";
    let mut h = machine();
    let mut c = Scripted::new(&[
        "edge1",
        "1",
        "",
        "",
        "corp.example",
        "",
        "svc",
        "",
        "admins",
        "pw",
        "",
        "",
        "",
        "",
        "y",                                                                    // files in S3
        "arn:aws:rolesanywhere:eu-west-1:123456789012:trust-anchor/not-a-uuid", // refused
        ta,
        ta, // refused: not a profile
        profile,
        "arn:aws:iam::123456789012:user/edge", // refused: not a role
        role,
        "", // the regional endpoint
        "", // OmniFile's edge CA issues the certificate (the default, decision 50)
        "y",
    ]);
    let a = prompt::questions(&mut c, &mut h, None).unwrap().answers;
    assert_eq!(a.aws.trust_anchor_arn, ta);
    assert_eq!(a.aws.profile_arn, profile);
    assert_eq!(a.aws.role_arn, role);
    assert!(!a.aws.client_ca() && a.aws.issuer.is_empty());
    assert!(c.said.contains("OmniFile's edge CA issue"));
    assert!(c.said.contains("certificate from OmniFile's edge CA"));
    assert!(a.problems().is_empty());
    assert!(
        c.said.matches("is not a").count() >= 3,
        "each bad ARN is refused: {}",
        c.said
    );
    assert!(
        c.said
            .contains(&format!("S3 reads       IAM Roles Anywhere as {role}"))
    );

    // The client's own CA stays an option: answer no, and it is kept as the
    // default the next time.
    let mut c = Scripted::new(&[
        "edge1",
        "1",
        "",
        "",
        "corp.example",
        "",
        "svc",
        "",
        "admins",
        "pw",
        "",
        "",
        "",
        "",
        "y",
        ta,
        profile,
        role,
        "",
        "n", // your own CA issues it
        "y",
    ]);
    let own = prompt::questions(&mut c, &mut h, None).unwrap().answers;
    assert!(own.aws.client_ca(), "{:?}", own.aws);
    assert!(c.said.contains("certificate from your own CA"));
    assert!(own.problems().is_empty());
}

#[test]
fn the_images_github_feed_is_offered_and_none_turns_updates_off() {
    let feed = "https://github.com/omnifile/omnifile-edge/releases/latest/download/";
    for (answer, want) in [("", feed), ("none", "")] {
        let mut h = machine();
        h.files.insert(
            omnifile_setup::IMAGE_FEED_FILE.into(),
            (format!("{feed}\n"), 0o644),
        );
        let mut c = Scripted::new(&[
            "edge1",
            "1",
            "",
            "",
            "corp.example",
            "",
            "svc",
            "",
            "admins",
            "pw",
            "",
            "",
            answer, // the update feed
            "",
            "",
            "y",
        ]);
        let a = prompt::questions(&mut c, &mut h, None).unwrap().answers;
        assert_eq!(a.updates.feed, want, "answer {answer:?}");
        assert!(c.said.contains(&format!("[{feed}]")), "offered: {}", c.said);
    }
}
