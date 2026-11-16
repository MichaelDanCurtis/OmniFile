//! Stubbing and recall fill never touch ownership, mode or ACLs
//! (access-control.md §1, "Stubs and recalls never touch permissions").
//!
//! Runs the real `omnifile-stub` handshake and fill on xfs, as root (the
//! `trusted.omnifile` marker needs CAP_SYS_ADMIN):
//!
//! ```text
//! sudo -E OMNIFILE_TEST_DIR=/srv/acl/test cargo test -p omnifile-acl --test stub_preserves_acl
//! ```
//!
//! Without root or xfs it prints a note and passes vacuously.
#![cfg(target_os = "linux")]

use std::fs::File;
use std::io;
use std::os::fd::AsFd;
use std::path::Path;

use omnifile_acl::{AclFs, Perms, ShareGroups, SysFs, Tag, apply_root, snapshot};
use omnifile_identity::fake::FakeIdMap;
use omnifile_marker::xattr;
use omnifile_marker::{Flags, Marker};
use omnifile_stub::fill::{FillOptions, FillRequest, fill};
use omnifile_stub::handshake::{StubRequest, stub};
use omnifile_stub::{
    FetchTarget, FileLocks, FsalNotifier, InodeKey, Layout, MarkerSeq, NoSmbGuard, RangeSource,
    StubOutcome, sys,
};

struct Fsal;
impl FsalNotifier for Fsal {
    async fn invalidate(&self, _: InodeKey, _: u32, _: bool) -> bool {
        true
    }
    fn fill_progress(&self, _: InodeKey, _: u64, _: u64) {}
    async fn begin_fill(&self, _: InodeKey, _: u64) -> Option<u64> {
        Some(1)
    }
    fn fence_holds(&self, _: u64) -> bool {
        true
    }
}

struct Mem(Vec<u8>);
impl RangeSource for Mem {
    async fn get_range(&self, _: &FetchTarget, off: u64, len: u64) -> io::Result<Vec<u8>> {
        Ok(self.0[off as usize..(off + len) as usize].to_vec())
    }
}

/// The SMB offline bit in Samba's `user.DOSATTRIB`, if it is there.
fn dos_offline(p: &Path) -> Option<bool> {
    let v = omnifile_acl::sys::lgetxattr(p, omnifile_marker::dosattr::NAME).unwrap()?;
    let a = omnifile_marker::dosattr::attrib(&v).expect("a DOSATTRIB layout we know");
    Some(a & omnifile_marker::dosattr::FILE_ATTRIBUTE_OFFLINE != 0)
}

/// Every xattr name except the stub marker and the offline bit, with its value.
fn xattrs_except_marker(p: &Path) -> Vec<(String, Vec<u8>)> {
    let c = std::ffi::CString::new(p.as_os_str().as_encoded_bytes()).unwrap();
    let mut buf = vec![0u8; 4096];
    // SAFETY: valid path and buffer.
    let n = unsafe { libc::llistxattr(c.as_ptr(), buf.as_mut_ptr().cast(), buf.len()) };
    assert!(n >= 0, "{}", io::Error::last_os_error());
    let mut out: Vec<(String, Vec<u8>)> = buf[..n as usize]
        .split(|b| *b == 0)
        .filter(|s| !s.is_empty())
        .map(|s| String::from_utf8_lossy(s).into_owned())
        // The marker, and the SMB offline bit that follows it (checked below).
        .filter(|n| n != "trusted.omnifile" && n != "user.DOSATTRIB")
        .map(|n| {
            let v = omnifile_acl::sys::lgetxattr(p, &n)
                .unwrap()
                .unwrap_or_default();
            (n, v)
        })
        .collect();
    out.sort();
    out
}

#[tokio::test]
async fn stub_and_fill_leave_owner_mode_and_acls_alone() {
    // SAFETY: no preconditions.
    if unsafe { libc::geteuid() } != 0 {
        eprintln!("skipping: needs root");
        return;
    }
    let base = std::env::var("OMNIFILE_TEST_DIR").unwrap_or_else(|_| "/srv/acl/test".into());
    if std::fs::create_dir_all(&base).is_err()
        || !sys::is_xfs(File::open(&base).unwrap().as_fd()).unwrap_or(false)
    {
        eprintln!("skipping: {base} is not xfs");
        return;
    }
    if sys::fanotify_guarded(std::path::Path::new(&base)).unwrap_or(false) {
        eprintln!("skipping: a running smb-guard watches {base}; use an unguarded xfs");
        return;
    }
    let dir = tempfile::tempdir_in(&base).unwrap();
    let layout = Layout::new(dir.path());
    let groups = ShareGroups {
        owners: 30003,
        writers: Some(30002),
        readers: Some(30001),
    };
    apply_root(&SysFs, dir.path(), &groups, None).unwrap();

    // A file geo-writer created over a share (inherits the share ACL), plus
    // an Explorer-style grant to geo-none.
    let p = dir.path().join("line_1042.segy");
    let data: Vec<u8> = (0..(6usize << 20))
        .map(|i| (i * 31 + i / 777) as u8)
        .collect();
    std::fs::write(&p, &data).unwrap();
    SysFs.chown(&p, Some(20002), Some(10000)).unwrap();
    let mut a = SysFs.read(&p).unwrap().access.unwrap();
    a.set(Tag::User(20003), Perms::R);
    a.recompute_mask();
    SysFs.write_access(&p, &a).unwrap();
    let mut m = Marker::resident(41, 410, data.len() as u64, 1);
    m.flags = Flags::ONLINE;
    xattr::write(File::open(&p).unwrap().as_fd(), &m).unwrap();

    let idmap = FakeIdMap::dev_ad();
    let before = SysFs.read(&p).unwrap();
    let before_sha = snapshot(&before, &idmap).unwrap().sha256;
    let before_x = xattrs_except_marker(&p);
    assert!(
        before
            .access
            .as_ref()
            .unwrap()
            .get(Tag::Group(30001))
            .is_some()
    );

    let seq = MarkerSeq::new();
    let req = StubRequest {
        rel: "line_1042.segy".into(),
        file_id: 41,
        version_id: 410,
        resident_bytes: 1 << 20,
        expect_ino: None,
        expect_ctime_ns: None,
    };
    let out = stub(&layout, &req, &Fsal, &NoSmbGuard, &seq, &FileLocks::new()).await;
    assert!(matches!(out, StubOutcome::Stubbed { .. }), "{out:?}");

    let stubbed = SysFs.read(&p).unwrap();
    assert_eq!(
        (stubbed.uid, stubbed.gid, stubbed.mode),
        (before.uid, before.gid, before.mode)
    );
    assert_eq!(stubbed.access, before.access, "access ACL after stub");
    assert_eq!(snapshot(&stubbed, &idmap).unwrap().sha256, before_sha);
    assert_eq!(xattrs_except_marker(&p), before_x, "no other xattr changed");
    assert_eq!(
        dos_offline(&p),
        Some(true),
        "a stub is offline to SMB clients"
    );

    let target = FetchTarget {
        file_id: 41,
        version_id: 410,
        url: "mem://".into(),
        source: omnifile_stub::SourceKind::AzureSas,
        size: data.len() as u64,
        crc64nvme: Some(omnifile_integrity::crc64_nvme(&data)),
    };
    let n = fill(
        &layout,
        &FillRequest {
            rel: "line_1042.segy".into(),
            expect_ino: None,
        },
        &target,
        &Mem(data.clone()),
        &Fsal,
        &seq,
        &FileLocks::new(),
        FillOptions {
            chunk: 1 << 20,
            widen_every: 2 << 20,
        },
    )
    .await
    .unwrap();
    assert_eq!(n, data.len() as u64);
    assert_eq!(std::fs::read(&p).unwrap(), data);

    let filled = SysFs.read(&p).unwrap();
    assert_eq!(
        (filled.uid, filled.gid, filled.mode),
        (before.uid, before.gid, before.mode)
    );
    assert_eq!(filled.access, before.access, "access ACL after fill");
    assert_eq!(snapshot(&filled, &idmap).unwrap().sha256, before_sha);
    assert_eq!(xattrs_except_marker(&p), before_x, "no other xattr changed");
    assert_eq!(dos_offline(&p), Some(false), "a filled file is not");
    assert!(!filled.has_ntacl);
    eprintln!("ACL preserved through stub and fill: {before_sha}");
}
