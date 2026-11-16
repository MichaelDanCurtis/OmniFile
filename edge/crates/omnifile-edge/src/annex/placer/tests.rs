//! The placer's steps and crash points, on a local directory standing for
//! the NAS, as NFSv3: the mode is the ACL. Each mount has its own root on that
//! directory (not its own superblock: only the exit test has a real NAS, and
//! with it a read-back that can differ from the write, and accounts that
//! differ). Linux only (the writes are `*at` calls on directory fds).
#![cfg(target_os = "linux")]

use std::os::unix::fs::{MetadataExt, PermissionsExt};
use std::sync::atomic::{AtomicBool, Ordering};

use serde_json::{Value, json};

use super::*;
use crate::annex::nasfs::open_root;
use crate::annex::service::Fake;
use omnifile_identity::fake::FakeIdMap;
use omnifile_identity::{IdMap, IdMapError, SidKind};

const OWNER: &str = "S-1-5-21-1-2-3-1105";
const GROUP: &str = "S-1-5-21-1-2-3-2001";
const URL: &str = "https://sim/blob/1?sig=x";

fn data(n: usize) -> Vec<u8> {
    (0..n).map(|i| (i * 7 % 251) as u8).collect()
}

fn crc(b: &[u8]) -> u64 {
    let mut c = omnifile_integrity::crc64::Crc64::new();
    c.update(b);
    c.finalize()
}

/// Identity lookups that can fail, as winbind's do: all of them, or all but the
/// next so many.
struct Flaky(FakeIdMap, AtomicBool, std::sync::atomic::AtomicI64);

impl Flaky {
    fn check(&self) -> Result<(), IdMapError> {
        let left = self.2.load(Ordering::SeqCst);
        if left >= 0 {
            self.2.store(left - 1, Ordering::SeqCst);
            if left == 0 {
                self.2.store(0, Ordering::SeqCst);
                return Err(IdMapError::Unavailable("test fault".into()));
            }
            return Ok(());
        }
        if self.1.load(Ordering::SeqCst) {
            Err(IdMapError::Unavailable("test fault".into()))
        } else {
            Ok(())
        }
    }
}

impl IdMap for Flaky {
    fn uid_to_sid(&self, uid: u32) -> Result<Option<Sid>, IdMapError> {
        self.check()?;
        self.0.uid_to_sid(uid)
    }
    fn gid_to_sid(&self, gid: u32) -> Result<Option<Sid>, IdMapError> {
        self.check()?;
        self.0.gid_to_sid(gid)
    }
    fn sid_to_uid(&self, sid: &Sid) -> Result<Option<u32>, IdMapError> {
        self.check()?;
        self.0.sid_to_uid(sid)
    }
    fn sid_to_gid(&self, sid: &Sid) -> Result<Option<u32>, IdMapError> {
        self.check()?;
        self.0.sid_to_gid(sid)
    }
    fn name_to_sid(&self, name: &str) -> Result<Option<(Sid, SidKind)>, IdMapError> {
        self.check()?;
        self.0.name_to_sid(name)
    }
    fn sid_to_name(&self, sid: &Sid) -> Result<Option<String>, IdMapError> {
        self.check()?;
        self.0.sid_to_name(sid)
    }
    fn user_token_sids(&self, user: &Sid) -> Result<Option<Vec<Sid>>, IdMapError> {
        self.check()?;
        self.0.user_token_sids(user)
    }
}

struct World {
    nas: tempfile::TempDir,
    state: tempfile::TempDir,
    db: AnnexDb,
    svc: Fake,
    ids: Flaky,
    now: std::cell::Cell<i64>,
    accounts: std::cell::RefCell<Option<AccountTrouble>>,
}

fn world() -> World {
    let nas = tempfile::tempdir().unwrap();
    std::fs::create_dir_all(nas.path().join("lts/surveys")).unwrap();
    std::fs::create_dir_all(nas.path().join("restore")).unwrap();
    // Folders others may add files to, as the requester may: `go` reads them.
    for d in ["lts", "lts/surveys", "restore"] {
        std::fs::set_permissions(nas.path().join(d), std::fs::Permissions::from_mode(0o775))
            .unwrap();
    }
    let uid = nix_uid();
    let gid = nix_gid();
    let mut ids = FakeIdMap::new("OMNI");
    ids.add_user("geo-owner", uid, OWNER)
        .add_group("geo", gid, GROUP);
    World {
        nas,
        state: tempfile::tempdir().unwrap(),
        db: AnnexDb::open_in_memory().unwrap(),
        svc: Fake::default(),
        ids: Flaky(
            ids,
            AtomicBool::new(false),
            std::sync::atomic::AtomicI64::new(-1),
        ),
        now: std::cell::Cell::new(1_000),
        accounts: std::cell::RefCell::new(None),
    }
}

fn nix_uid() -> u32 {
    // SAFETY: no arguments.
    unsafe { libc::getuid() }
}

fn nix_gid() -> u32 {
    // SAFETY: no arguments.
    unsafe { libc::getgid() }
}

fn ticket(pid: u64, folder: &str, name: &str, body: &[u8], origin: bool) -> Value {
    json!({
        "placement_id": pid.to_string(), "file_id": "9", "version_id": "3",
        "path": format!("surveys/{name}"), "size": body.len(), "crc64": format!("{:016x}", crc(body)),
        "mtime_ns": "1700000000000000000", "folder": folder,
        "names": [name, "a-restored.segy", "a-restored-2026-11-05-120000Z.segy"],
        "origin": origin, "same_content": false, "protect": !origin,
        "acl": {"kind": "posix", "mode": 0o640, "owner_sid": OWNER, "group_sid": GROUP},
        "read": {"mode": "azure_user_delegation_sas", "url": URL},
        "uid": null, "gid": null,
        "expires_at": "2099-01-01T00:00:00Z"
    })
}

fn give(w: &World, t: Value, body: &[u8]) {
    let mut s = w.svc.state.lock().unwrap();
    s.placement_tickets.push(t);
    s.read_blobs.insert(URL.into(), body.to_vec());
}

async fn pass(w: &World) -> Pass {
    placer_pass(w, true).await
}

/// The staging folder of the test's annex.
const ANNEX: u64 = 7;

/// A3.4's grace in these tests (ms).
const GRACE_MS: i64 = 60_000;

fn staging(w: &World) -> std::path::PathBuf {
    w.nas
        .path()
        .join(w.db.meta("staging_rel").unwrap().unwrap_or_default())
}

/// The folders on the NAS with a staging folder's name.
fn staging_folders(w: &World) -> Vec<String> {
    std::fs::read_dir(w.nas.path())
        .unwrap()
        .map(|e| e.unwrap().file_name().to_string_lossy().into_owned())
        .filter(|n| n.starts_with(".omnifile-staging"))
        .collect()
}

/// The next pass runs an hour on: what waits (stuck, backing off) is due.
fn later(w: &World) {
    w.now.set(w.now.get() + STUCK_RETRY_MS + 1);
}

async fn placer_pass(w: &World, ask: bool) -> Pass {
    if ask {
        take_tickets(&w.svc, &w.db, ANNEX, w.now.get()).await;
    }
    let pl = open_root(w.nas.path(), "").unwrap();
    let ro = open_root(w.nas.path(), "").unwrap();
    let p = Placer {
        svc: &w.svc,
        db: &w.db,
        pl: &pl,
        ro: &ro,
        annex_id: ANNEX,
        annex_name: "lts",
        annex_rel: "lts",
        flavour: Flavour::Nfs3,
        ids: &w.ids,
        placement_sid: None,
        read_sid: None,
        read_who: None,
        now: w.now.get(),
        state_dir: w.state.path(),
        accounts_problem: w.accounts.borrow().clone(),
        journal_failed: std::cell::Cell::new(false),
        grace_ms: GRACE_MS,
        smb: SMB_ON.with(|x| x.get()),
    };
    p.pass().await
}

/// A placer as a pass makes it, for one call.
fn with_placer<R>(w: &World, f: impl FnOnce(&Placer<'_, Fake>) -> R) -> R {
    let pl = open_root(w.nas.path(), "").unwrap();
    let ro = open_root(w.nas.path(), "").unwrap();
    let p = Placer {
        svc: &w.svc,
        db: &w.db,
        pl: &pl,
        ro: &ro,
        annex_id: ANNEX,
        annex_name: "lts",
        annex_rel: "lts",
        flavour: Flavour::Nfs3,
        ids: &w.ids,
        placement_sid: None,
        read_sid: None,
        read_who: None,
        now: w.now.get(),
        state_dir: w.state.path(),
        accounts_problem: w.accounts.borrow().clone(),
        journal_failed: std::cell::Cell::new(false),
        grace_ms: GRACE_MS,
        smb: SMB_ON.with(|x| x.get()),
    };
    f(&p)
}

fn state(w: &World, pid: u64) -> Placement {
    Journal(&w.db).get(pid).unwrap().unwrap()
}

fn steps(w: &World) -> Vec<String> {
    w.svc
        .state
        .lock()
        .unwrap()
        .placement_steps
        .iter()
        .map(|(_, b)| b["step"].as_str().unwrap_or("").to_owned())
        .collect()
}

fn staging_empty(w: &World) -> bool {
    match w.db.meta("staging_rel").unwrap() {
        Some(r) if !r.is_empty() => std::fs::read_dir(w.nas.path().join(r))
            .map(|d| d.count() == 0)
            .unwrap_or(true),
        _ => true,
    }
}

#[tokio::test]
async fn a_placement_back_where_it_came_from() {
    let w = world();
    let body = data(3 << 20);
    give(&w, ticket(1, "lts/surveys", "a.segy", &body, true), &body);
    let out = pass(&w).await;
    assert_eq!(out.placed, 1, "{:?}", state(&w, 1));
    let placed = w.nas.path().join("lts/surveys/a.segy");
    assert_eq!(std::fs::read(&placed).unwrap(), body);
    let md = std::fs::metadata(&placed).unwrap();
    assert_eq!(md.mode() & 0o7777, 0o640, "its own mode");
    assert_eq!(md.mtime(), 1_700_000_000, "its own mtime");
    assert_eq!(md.nlink(), 1);
    assert!(staging_empty(&w), "nothing left in the staging folder");
    // The staging folder is locked down, and remembered.
    let sm = std::fs::metadata(staging(&w)).unwrap();
    assert_eq!(sm.mode() & 0o077, 0);
    assert_eq!(steps(&w), ["filled", "read_back", "go", "done"]);
    let done = &w.svc.state.lock().unwrap().placement_steps[3].1;
    assert_eq!(done["outcome"], "placed");
    assert_eq!(done["name"], "a.segy");
    assert_eq!(done["attrs"]["nas_file_id"], md.ino().to_string());
}

#[tokio::test]
async fn a_name_taken_takes_the_restored_name_and_never_replaces() {
    let w = world();
    let body = data(1000);
    std::fs::write(w.nas.path().join("lts/surveys/a.segy"), b"the user's").unwrap();
    give(&w, ticket(1, "lts/surveys", "a.segy", &body, true), &body);
    pass(&w).await;
    assert_eq!(
        std::fs::read(w.nas.path().join("lts/surveys/a.segy")).unwrap(),
        b"the user's"
    );
    assert_eq!(
        std::fs::read(w.nas.path().join("lts/surveys/a-restored.segy")).unwrap(),
        body
    );
    assert_eq!(state(&w, 1).outcome.as_deref(), Some("restored_name"));
}

#[tokio::test]
async fn same_content_leaves_the_same_bytes_alone_and_only_those() {
    let body = data(1000);
    // The same bytes under the name: nothing written, nothing left behind.
    let w = world();
    let name = w.nas.path().join("lts/surveys/a.segy");
    std::fs::write(&name, &body).unwrap();
    let ino = std::fs::metadata(&name).unwrap().ino();
    let ptr = w.nas.path().join("lts/surveys/a.segy.omnifile.txt");
    let text =
        crate::annex::remover::pointer_text("lts", "surveys/a.segy", 9, "2026-10-01T00:00:00Z");
    std::fs::write(&ptr, &text).unwrap();
    let mut t = ticket(1, "lts/surveys", "a.segy", &body, true);
    t["same_content"] = json!(true);
    give(&w, t, &body);
    pass(&w).await;
    let p = state(&w, 1);
    assert_eq!(
        (p.state.as_str(), p.outcome.as_deref()),
        ("done", Some("already_there"))
    );
    assert_eq!(std::fs::metadata(&name).unwrap().ino(), ino, "untouched");
    // `done` says what it saw, for the file to come back as, and its note goes.
    assert_eq!(last_done(&w)["attrs"]["nas_file_id"], ino.to_string());
    assert!(!ptr.exists(), "it is back under its own name");
    assert_eq!(p.pointer_deleted, Some(true));
    assert!(!w.nas.path().join("lts/surveys/a-restored.segy").exists());
    assert!(staging_empty(&w));
    // Other bytes of the same size: never taken for the same, a -restored name.
    let w = world();
    let name = w.nas.path().join("lts/surveys/a.segy");
    let mut other = body.clone();
    other[500] ^= 0xff;
    std::fs::write(&name, &other).unwrap();
    let mut t = ticket(1, "lts/surveys", "a.segy", &body, true);
    t["same_content"] = json!(true);
    give(&w, t, &body);
    pass(&w).await;
    assert_eq!(state(&w, 1).outcome.as_deref(), Some("restored_name"));
    assert_eq!(std::fs::read(&name).unwrap(), other);
    assert_eq!(
        std::fs::read(w.nas.path().join("lts/surveys/a-restored.segy")).unwrap(),
        body
    );
}

#[tokio::test]
async fn a_name_made_by_a_user_right_before_the_link_is_never_replaced() {
    for refused in [false, true] {
        let w = world();
        let body = data(1000);
        give(&w, ticket(1, "lts/surveys", "a.segy", &body, true), &body);
        let p = w.nas.path().join("lts/surveys/a.segy");
        let p2 = p.clone();
        faults::BEFORE_P4.with(|h| {
            *h.borrow_mut() = Some(Box::new(move || {
                std::fs::write(&p2, b"just saved").unwrap();
            }))
        });
        // Through the link, and through the placeholder (a link refused).
        faults::LINK_EACCES.with(|f| f.set(refused));
        pass(&w).await;
        faults::LINK_EACCES.with(|f| f.set(false));
        assert_eq!(
            std::fs::read(&p).unwrap(),
            b"just saved",
            "refused={refused}"
        );
        assert_eq!(
            std::fs::read(w.nas.path().join("lts/surveys/a-restored.segy")).unwrap(),
            body
        );
    }
}

#[tokio::test]
async fn a_staging_file_changed_after_it_was_secured_is_never_linked() {
    let w = world();
    let body = data(1000);
    give(&w, ticket(1, "lts/surveys", "a.segy", &body, true), &body);
    faults::STOP_AT.with(|s| *s.borrow_mut() = Some("secured".into()));
    pass(&w).await;
    assert_eq!(state(&w, 1).state, "secured");
    // Someone (it would take the placement account) writes to it.
    let st = state(&w, 1);
    let f = staging(&w).join(&st.staging_name);
    std::fs::OpenOptions::new().append(true).open(&f).unwrap();
    std::fs::write(&f, b"tampered").unwrap();
    placer_pass(&w, false).await;
    assert_eq!(state(&w, 1).state, "failed");
    assert_eq!(state(&w, 1).reason.as_deref(), Some("staging_file_changed"));
    assert!(!w.nas.path().join("lts/surveys/a.segy").exists());
}

#[tokio::test]
async fn a_flipped_byte_is_caught_by_the_read_back_then_tried_once_more() {
    let w = world();
    let body = data(1000);
    give(&w, ticket(1, "lts/surveys", "a.segy", &body, true), &body);
    faults::CRC_FLIP.with(|f| f.set(true));
    pass(&w).await;
    // The second try is good.
    assert_eq!(state(&w, 1).state, "done");
    assert_eq!(
        std::fs::read(w.nas.path().join("lts/surveys/a.segy")).unwrap(),
        body
    );
    assert!(staging_empty(&w));
}

#[tokio::test]
async fn a_refused_read_back_fails_closed() {
    let w = world();
    let body = data(1000);
    give(&w, ticket(1, "lts/surveys", "a.segy", &body, true), &body);
    faults::READBACK_EACCES.with(|f| f.set(true));
    pass(&w).await;
    let p = state(&w, 1);
    assert_eq!(
        (p.state.as_str(), p.reason.as_deref()),
        ("failed", Some("readback_denied"))
    );
    assert!(staging_empty(&w));
    assert!(!w.nas.path().join("lts/surveys/a.segy").exists());
}

#[tokio::test]
async fn the_service_refusing_go_leaves_nothing_behind() {
    let w = world();
    let body = data(1000);
    give(&w, ticket(1, "lts/surveys", "a.segy", &body, true), &body);
    w.svc.state.lock().unwrap().refuse_go = Some("acl_mismatch".into());
    pass(&w).await;
    let p = state(&w, 1);
    assert_eq!(
        (p.state.as_str(), p.reason.as_deref()),
        ("failed", Some("acl_mismatch"))
    );
    assert!(staging_empty(&w));
    assert!(!w.nas.path().join("lts/surveys/a.segy").exists());
}

#[tokio::test]
async fn a_full_disk_fails_cleanly() {
    let w = world();
    let body = data(1000);
    give(&w, ticket(1, "lts/surveys", "a.segy", &body, true), &body);
    faults::ENOSPC_AT.with(|f| f.set(1));
    pass(&w).await;
    let p = state(&w, 1);
    assert_eq!(
        (p.state.as_str(), p.reason.as_deref()),
        ("failed", Some("no_space"))
    );
    assert!(staging_empty(&w));
}

#[tokio::test]
async fn too_little_room_removes_the_folders_that_same_step_made() {
    // The found-on-a-full-NAS case: P0 makes the folders, and the space check
    // right after it fails, in the one step.
    let w = world();
    let body = data(1000);
    let mut t = ticket(1, "restore/x/y", "a.segy", &body, false);
    t["size"] = json!(1u64 << 62);
    give(&w, t, &body);
    pass(&w).await;
    assert_eq!(state(&w, 1).reason.as_deref(), Some("no_space"));
    assert!(!w.nas.path().join("restore/x").exists());
    assert!(w.nas.path().join("restore").exists(), "the location stays");
}

#[tokio::test]
async fn a_restore_folder_gets_its_missing_folders_and_a_failure_removes_them() {
    let w = world();
    let body = data(1000);
    give(
        &w,
        ticket(1, "restore/surveys/2026", "a.segy", &body, false),
        &body,
    );
    pass(&w).await;
    assert_eq!(
        std::fs::read(w.nas.path().join("restore/surveys/2026/a.segy")).unwrap(),
        body
    );
    // A failure after P0: the folders it made go, empty.
    let w = world();
    give(&w, ticket(2, "restore/x/y", "a.segy", &body, false), &body);
    w.svc.state.lock().unwrap().refuse_go = Some("access_denied".into());
    pass(&w).await;
    assert!(!w.nas.path().join("restore/x").exists());
    // The origin's folder is never made.
    let w = world();
    give(&w, ticket(3, "lts/gone", "a.segy", &body, true), &body);
    pass(&w).await;
    assert_eq!(state(&w, 3).reason.as_deref(), Some("target_missing"));
    assert!(!w.nas.path().join("lts/gone").exists());
}

#[tokio::test]
async fn a_crash_at_every_step_resumes_and_never_leaves_a_partial_file_under_a_name() {
    for at in [
        "folders_ready",
        "create",
        "created",
        "acl_set",
        "secured",
        "linking",
        "link_taken",
        "linked",
        "owned",
    ] {
        let w = world();
        let body = data(2 << 20);
        give(&w, ticket(1, "lts/surveys", "a.segy", &body, true), &body);
        faults::STOP_AT.with(|s| *s.borrow_mut() = Some(at.into()));
        pass(&w).await;
        let name = w.nas.path().join("lts/surveys/a.segy");
        // Whatever is under the name is the whole file, or nothing.
        if name.exists() {
            assert_eq!(std::fs::read(&name).unwrap(), body, "after a crash at {at}");
        }
        placer_pass(&w, false).await;
        assert_eq!(
            state(&w, 1).state,
            "done",
            "resumed from {at}: {:?}",
            state(&w, 1)
        );
        assert_eq!(std::fs::read(&name).unwrap(), body, "from {at}");
        assert!(staging_empty(&w), "from {at}");
        assert!(
            !w.nas.path().join("lts/surveys/a-restored.segy").exists(),
            "from {at}"
        );
    }
}

#[tokio::test]
async fn a_crash_between_the_create_and_its_journal_accepts_only_an_empty_file_of_ours() {
    let w = world();
    let body = data(1000);
    give(&w, ticket(1, "lts/surveys", "a.segy", &body, true), &body);
    faults::STOP_AT.with(|s| *s.borrow_mut() = Some("create".into()));
    pass(&w).await;
    let st = state(&w, 1);
    assert_eq!(st.state, "create");
    // The create ran; its journal didn't: an empty file of ours is ours.
    std::fs::write(staging(&w).join(&st.staging_name), b"").unwrap();
    placer_pass(&w, false).await;
    assert_eq!(state(&w, 1).state, "done");
    // Something else at that name: never touched, an owner looks.
    let w = world();
    give(&w, ticket(2, "lts/surveys", "a.segy", &body, true), &body);
    faults::STOP_AT.with(|s| *s.borrow_mut() = Some("create".into()));
    pass(&w).await;
    let st = state(&w, 2);
    let f = staging(&w).join(&st.staging_name);
    std::fs::write(&f, b"not ours").unwrap();
    placer_pass(&w, false).await;
    assert_eq!(state(&w, 2).state, "stuck");
    assert_eq!(std::fs::read(&f).unwrap(), b"not ours");
}

#[tokio::test]
async fn a_fill_resumed_on_a_swapped_staging_file_never_links_it() {
    // R5: P2 resumes only on the journal's inode, one link and the ACL it
    // was born with; a file put in its place is never filled or linked.
    for swap in ["replaced", "linked"] {
        let w = world();
        let body = data(1000);
        give(&w, ticket(1, "lts/surveys", "a.segy", &body, true), &body);
        faults::STOP_AT.with(|s| *s.borrow_mut() = Some("created".into()));
        pass(&w).await;
        let st = state(&w, 1);
        assert_eq!(st.state, "created");
        let f = staging(&w).join(&st.staging_name);
        if swap == "replaced" {
            std::fs::remove_file(&f).unwrap();
            std::fs::write(&f, b"").unwrap();
        } else {
            std::fs::hard_link(&f, w.nas.path().join("lts/elsewhere")).unwrap();
        }
        placer_pass(&w, false).await;
        let p = state(&w, 1);
        assert_eq!(
            (p.state.as_str(), p.reason.as_deref()),
            ("failed", Some("staging_file_changed")),
            "{swap}"
        );
        assert!(!w.nas.path().join("lts/surveys/a.segy").exists(), "{swap}");
    }
}

#[tokio::test]
async fn an_edited_placeholder_is_never_replaced() {
    // P4a: where the link is refused, a placeholder holds the name; one a
    // user edited before the rename stops the placement (stuck), untouched.
    let w = world();
    let body = data(1000);
    give(&w, ticket(1, "lts/surveys", "a.segy", &body, true), &body);
    faults::LINK_EACCES.with(|f| f.set(true));
    faults::STOP_AT.with(|s| *s.borrow_mut() = Some("placeholder".into()));
    pass(&w).await;
    faults::LINK_EACCES.with(|f| f.set(false));
    assert_eq!(state(&w, 1).state, "placeholder");
    let name = w.nas.path().join("lts/surveys/a.segy");
    std::fs::write(&name, b"a user's work").unwrap();
    placer_pass(&w, false).await;
    let p = state(&w, 1);
    assert_eq!(
        (p.state.as_str(), p.reason.as_deref()),
        ("stuck", Some("placeholder_changed"))
    );
    assert_eq!(std::fs::read(&name).unwrap(), b"a user's work");
}

#[tokio::test]
async fn a_staging_folder_omnifile_didnt_make_is_never_adopted() {
    // Under the name the journal chose, with the mkdir never recorded: a crash,
    // or someone quick. It holds nothing of ours, and it holds nothing up.
    let w = world();
    let old = staging_dir(ANNEX, "ab12");
    std::fs::create_dir(w.nas.path().join(&old)).unwrap();
    std::fs::write(w.nas.path().join(&old).join("x"), b"a user's").unwrap();
    w.db.set_meta("staging_rel", &old).unwrap();
    let body = data(1000);
    give(&w, ticket(1, "lts/surveys", "a.segy", &body, true), &body);
    pass(&w).await;
    let p = state(&w, 1);
    assert_eq!(p.state, "done", "{p:?}");
    assert_ne!(w.db.meta("staging_rel").unwrap().unwrap(), old);
    assert_eq!(
        std::fs::read(w.nas.path().join(&old).join("x")).unwrap(),
        b"a user's"
    );
    // Made by OmniFile, then changed by someone: every placement stops.
    let w = world();
    give(&w, ticket(1, "lts/surveys", "a.segy", &body, true), &body);
    pass(&w).await;
    std::fs::set_permissions(staging(&w), std::fs::Permissions::from_mode(0o777)).unwrap();
    give(&w, ticket(2, "lts/surveys", "b.segy", &body, true), &body);
    pass(&w).await;
    assert!(
        state(&w, 2)
            .reason
            .unwrap_or_default()
            .starts_with("staging_changed")
    );
}

#[tokio::test]
async fn a_journal_that_cant_be_written_stops_before_the_write() {
    let w = world();
    let body = data(1000);
    give(&w, ticket(1, "lts/surveys", "a.segy", &body, true), &body);
    faults::JOURNAL_FAILS.with(|f| f.set(true));
    pass(&w).await;
    faults::JOURNAL_FAILS.with(|f| f.set(false));
    assert_eq!(state(&w, 1).state, "ticketed");
    assert!(staging_empty(&w));
    assert!(
        staging_folders(&w).is_empty(),
        "no mkdir before its name is journalled"
    );
    assert!(!w.nas.path().join("lts/surveys/a.segy").exists());
}

#[tokio::test]
async fn the_pointer_goes_only_when_it_is_ours() {
    for (ours, edited) in [(true, false), (true, true), (false, false)] {
        let w = world();
        let body = data(1000);
        let ptr = w.nas.path().join("lts/surveys/a.segy.omnifile.txt");
        let text =
            crate::annex::remover::pointer_text("lts", "surveys/a.segy", 9, "2026-10-01T00:00:00Z");
        std::fs::write(&ptr, if ours { text.as_str() } else { "a user's note" }).unwrap();
        if edited {
            std::fs::write(&ptr, format!("{text}my notes\n")).unwrap();
        }
        give(&w, ticket(1, "lts/surveys", "a.segy", &body, true), &body);
        pass(&w).await;
        assert_eq!(state(&w, 1).state, "done");
        assert_eq!(ptr.exists(), !ours || edited, "ours={ours} edited={edited}");
    }
}

#[test]
fn ticket_names_are_plain_and_never_omnifiles_own() {
    let body = data(10);
    let mut t = ticket(1, "lts", "a.segy", &body, true);
    assert!(Ticket::from_json(&t).is_ok());
    t["names"] = json!(["../x"]);
    assert!(Ticket::from_json(&t).is_err());
    t["names"] = json!([".omnifile-removing-1-x"]);
    assert!(Ticket::from_json(&t).is_err());
    t["names"] = json!(["a.segy"]);
    t["folder"] = json!(".omnifile-staging");
    assert!(Ticket::from_json(&t).is_err());
    t["folder"] = json!("a/../b");
    assert!(Ticket::from_json(&t).is_err());
}

#[test]
fn an_nt_descriptor_placed_elsewhere_is_protected_and_explicit() {
    // An SD with one inherited ACE.
    let sid = [1u8, 1, 0, 0, 0, 0, 0, 1, 0, 0, 0, 0]; // S-1-1-0
    let mut ace = vec![0u8, 0x10, (8 + sid.len()) as u8, 0];
    ace.extend_from_slice(&0x0012_0089u32.to_le_bytes());
    ace.extend_from_slice(&sid);
    let mut sd = vec![1u8, 0, 0x04, 0x80];
    sd.extend_from_slice(&0u32.to_le_bytes());
    sd.extend_from_slice(&0u32.to_le_bytes());
    sd.extend_from_slice(&0u32.to_le_bytes());
    sd.extend_from_slice(&20u32.to_le_bytes());
    sd.extend_from_slice(&[2, 0, (8 + ace.len()) as u8, 0, 1, 0, 0, 0]);
    sd.extend_from_slice(&ace);
    let out = imp::nt_descriptor(&sd, true).unwrap();
    assert_ne!(
        u16::from_le_bytes([out[2], out[3]]) & 0x1000,
        0,
        "protected"
    );
    assert_eq!(out[28 + 1] & 0x10, 0, "no longer inherited");
    assert_eq!(
        imp::nt_descriptor(&sd, false).unwrap(),
        sd,
        "its own folder: exactly as it was"
    );
    // The staging folder's DACL names only its two accounts.
    let d = imp::dacl_sd(&[("S-1-5-21-1-2-3-500".into(), 0x001F_01FF)], true).unwrap();
    let v = json!({"kind": "ntfs_sd", "raw": base64_of(&d)});
    assert!(imp::locked(
        &v,
        Flavour::Smb,
        Some("S-1-5-21-1-2-3-500"),
        None,
        None
    ));
    assert!(!imp::locked(
        &v,
        Flavour::Smb,
        Some("S-1-5-21-1-2-3-501"),
        None,
        None
    ));
}

fn base64_of(b: &[u8]) -> String {
    use base64::Engine;
    base64::engine::general_purpose::STANDARD.encode(b)
}

#[test]
fn a_pointer_is_known_only_by_its_exact_text() {
    let t = crate::annex::remover::pointer_text("lts", "a/b.segy", 9, "2026-10-01T00:00:00Z");
    assert!(is_pointer_text(t.as_bytes(), "lts", "a/b.segy", 9));
    assert!(!is_pointer_text(
        format!("{t} ").as_bytes(),
        "lts",
        "a/b.segy",
        9
    ));
    assert!(!is_pointer_text(t.as_bytes(), "lts", "a/b.segy", 10));
}

// --- PR #70's review. Each of these fails without its fix.

#[tokio::test]
async fn a_folder_after_a_failed_mkdir_is_never_adopted() {
    // E1, and what the name left unjournalled did: a crash, a replayed mkdir's
    // EEXIST or a failed set_meta stuck every placement until someone removed it.
    let body = data(1000);
    for fault in ["replayed", "unrecorded"] {
        let w = world();
        give(&w, ticket(1, "lts/surveys", "a.segy", &body, true), &body);
        match fault {
            "replayed" => faults::MKDIR_REPLAYED.with(|f| f.set(true)),
            _ => faults::MADE_NOT_RECORDED.with(|f| f.set(true)),
        }
        pass(&w).await;
        let p = state(&w, 1);
        assert_eq!(p.state, "ticketed", "{fault}, not stuck: {p:?}");
        assert_eq!(w.db.meta("staging_ino").unwrap(), None, "{fault}");
        let first = w.db.meta("staging_rel").unwrap().unwrap();
        assert!(w.nas.path().join(&first).is_dir(), "{fault}");
        later(&w);
        placer_pass(&w, false).await;
        let p = state(&w, 1);
        assert_eq!(p.state, "done", "{fault}: {p:?}");
        assert_ne!(w.db.meta("staging_rel").unwrap().unwrap(), first, "{fault}");
        assert_eq!(staging_folders(&w).len(), 1, "{fault}: the empty one went");
    }
    // Another folder under the recorded mkdir's name: stops, is never used.
    let w = world();
    let rel = staging_dir(ANNEX, "ab12");
    std::fs::create_dir(w.nas.path().join(&rel)).unwrap();
    w.db.set_meta("staging_rel", &rel).unwrap();
    w.db.set_meta("staging_made", "1").unwrap();
    give(&w, ticket(1, "lts/surveys", "a.segy", &body, true), &body);
    pass(&w).await;
    let p = state(&w, 1);
    assert_eq!(p.state, "stuck", "{p:?}");
    assert!(
        p.reason
            .unwrap_or_default()
            .starts_with("staging_unexpected")
    );
    assert_eq!(w.db.meta("staging_ino").unwrap(), None);
}

#[tokio::test]
async fn a_staging_file_that_wont_lock_never_gets_a_byte() {
    // E1: nothing is taken from what a new file inherited.
    let w = world();
    let body = data(1000);
    give(&w, ticket(1, "lts/surveys", "a.segy", &body, true), &body);
    faults::BORN_WIDE.with(|f| f.set(true));
    pass(&w).await;
    let p = state(&w, 1);
    assert_eq!(
        (p.state.as_str(), p.reason.as_deref()),
        ("failed", Some("staging_file_not_locked"))
    );
    assert!(staging_empty(&w));
    assert!(!w.nas.path().join("lts/surveys/a.segy").exists());
}

#[test]
fn the_lock_is_read_back_whatever_the_flavour() {
    use base64::Engine;
    const P: &str = "S-1-5-21-1-2-3-1112";
    const R: &str = "S-1-5-21-1-2-3-1108";
    const OTHER: &str = "S-1-5-21-1-2-3-513";
    let raw = |aces: &[(&str, u32, u8)]| {
        let sd = imp::dacl_sd(
            &aces
                .iter()
                .map(|(s, m, _)| ((*s).to_owned(), *m))
                .collect::<Vec<_>>(),
            false,
        )
        .unwrap();
        let mut sd = sd;
        // Each ACE's flags, in place (20: the DACL; 8: its header).
        let mut at = 28;
        for (_, _, f) in aces {
            sd[at + 1] = *f;
            at += u16::from_le_bytes([sd[at + 2], sd[at + 3]]) as usize;
        }
        json!({"kind": "ntfs_sd", "raw": base64::engine::general_purpose::STANDARD.encode(sd)})
    };
    let smb = |a: &Value| imp::locked(a, Flavour::Smb, Some(P), Some(R), None);
    assert!(smb(&raw(&[(P, 0x1F_01FF, 0), (R, 0x12_00A9, 0)])));
    // Inherit-only entries are what new files get (minor, inherit-only ACEs).
    assert!(smb(&raw(&[
        (P, 0x1F_01FF, 0),
        ("S-1-3-0", 0x1F_01FF, 0x08)
    ])));
    assert!(!smb(&raw(&[(P, 0x1F_01FF, 0), (OTHER, 0x12_00A9, 0x08)])));
    assert!(!smb(&raw(&[
        (P, 0x1F_01FF, 0),
        ("S-1-3-1", 0x12_00A9, 0x08)
    ])));
    assert!(!smb(&raw(&[(P, 0x1F_01FF, 0), (OTHER, 0x12_00A9, 0)])));
    assert!(
        smb(&raw(&[(P, 0x1F_01FF, 0), (OTHER, 0, 0x08)])),
        "grants nothing"
    );
    // NFSv4 (E2): only OWNER@ and the read account, inherited entries too.
    let nfs4 = |xdr: Vec<u8>| json!({"kind": "nfs4", "raw": base64::engine::general_purpose::STANDARD.encode(xdr)});
    let who = Some("reader@dom");
    let v4 = |a: &Value| imp::locked(a, Flavour::Nfs4, None, None, who);
    assert!(v4(&nfs4(imp::nfs4_lock(who, true))));
    assert!(v4(&nfs4(imp::nfs4_lock(None, false))));
    assert!(!v4(&nfs4(imp::nfs4_lock(Some("group:geo@dom"), true))));
    // NFSv3, or an NFSv4 server with no ACLs: the mode, and no named entry.
    let posix = |mode: u32, e: Value| json!({"kind": "posix", "mode": mode, "entries": e});
    let v3 = |a: &Value| imp::locked(a, Flavour::Nfs3, None, None, None);
    assert!(v3(&posix(0o700, json!([]))));
    assert!(!v3(&posix(0o750, json!([]))));
    assert!(!v3(&posix(
        0o700,
        json!([{"tag": "group", "perms": "r-x"}])
    )));
    assert!(imp::locked(
        &posix(0o700, json!([])),
        Flavour::Nfs4,
        None,
        None,
        None
    ));
    assert!(!imp::locked(
        &posix(0o700, json!([])),
        Flavour::Smb,
        None,
        None,
        None
    ));
}

#[tokio::test]
async fn a_read_credential_that_runs_out_is_renewed_during_the_fill() {
    // E4: it lasts an hour, and the edge used to throw away the new one.
    const URL2: &str = "https://sim/blob/1?sig=new";
    for (near, why) in [(true, "by its expiry"), (false, "by the refusal")] {
        let w = world();
        let body = data(1 << 20);
        let mut t = ticket(1, "lts/surveys", "a.segy", &body, true);
        if near {
            t["expires_at"] = json!("2000-01-01T00:00:00.000Z");
        }
        give(&w, t, &body);
        {
            let mut s = w.svc.state.lock().unwrap();
            s.read_blobs.insert(URL2.into(), body.clone());
            s.expired_urls.push(URL.into());
            s.renewed_read = Some((URL2.into(), "2099-01-01T00:00:00.000Z".into()));
        }
        pass(&w).await;
        assert_eq!(state(&w, 1).state, "done", "{why}");
        assert_eq!(
            std::fs::read(w.nas.path().join("lts/surveys/a.segy")).unwrap(),
            body
        );
        if near {
            // Renewed ahead of its expiry: not rescued by a refusal afterwards.
            let reads = w.svc.state.lock().unwrap().read_urls.clone();
            assert!(!reads.is_empty());
            assert!(!reads.iter().any(|u| u == URL), "{reads:?}");
        }
        let asks = w.svc.state.lock().unwrap().placement_asks.clone();
        let renew = asks.iter().find(|a| a["renew"][0] == "1").unwrap();
        assert_eq!(renew["in_hand"][0], "1", "{why}");
        let v: Value = serde_json::from_str(&state(&w, 1).ticket).unwrap();
        assert_eq!(v["read"]["url"], URL2, "journalled, for a resume");
    }
    // Nothing new to be had: tried again later, nothing else happens.
    let w = world();
    let body = data(1000);
    give(&w, ticket(1, "lts/surveys", "a.segy", &body, true), &body);
    w.svc.state.lock().unwrap().expired_urls.push(URL.into());
    pass(&w).await;
    assert_eq!(state(&w, 1).state, "filling");
    assert!(state(&w, 1).next_at > 1_000);
}

#[tokio::test]
async fn what_is_in_hand_is_said_and_a_ticket_given_again_only_renews() {
    // S10's other half: the edge says what it has, so nothing is minted for it,
    // and a ticket handed over again changes only the credential.
    let w = world();
    let body = data(1000);
    give(&w, ticket(1, "lts/surveys", "a.segy", &body, true), &body);
    faults::STOP_AT.with(|s| *s.borrow_mut() = Some("created".into()));
    pass(&w).await;
    let mut again = ticket(1, "elsewhere", "other.segy", &body, true);
    again["read"]["url"] = json!("https://sim/blob/1?sig=again");
    again["expires_at"] = json!("2098-01-01T00:00:00.000Z");
    w.svc.state.lock().unwrap().placement_tickets.push(again);
    take_tickets(&w.svc, &w.db, ANNEX, 2_000).await;
    let asks = w.svc.state.lock().unwrap().placement_asks.clone();
    assert_eq!(asks.last().unwrap()["in_hand"][0], "1");
    let v: Value = serde_json::from_str(&state(&w, 1).ticket).unwrap();
    assert_eq!(v["read"]["url"], "https://sim/blob/1?sig=again");
    assert_eq!(v["folder"], "lts/surveys", "only the credential");
}

#[tokio::test]
async fn a_nas_hiccup_isnt_an_owner_problem_and_stuck_goes_on_from_its_step() {
    // E5: any staging error used to stick a placement, for good.
    let w = world();
    let body = data(1000);
    give(&w, ticket(1, "lts/surveys", "a.segy", &body, true), &body);
    faults::STOP_AT.with(|s| *s.borrow_mut() = Some("filled".into()));
    pass(&w).await;
    assert_eq!(state(&w, 1).state, "filled");
    // Winbind away, the NAS away: the same step, later.
    faults::STAGING_EIO.with(|f| f.set(true));
    placer_pass(&w, false).await;
    faults::STAGING_EIO.with(|f| f.set(false));
    let p = state(&w, 1);
    assert_eq!(p.state, "filled", "tried again, not stuck: {p:?}");
    assert!(p.next_at > w.now.get());
    // A folder someone opened up is an owner's problem, and stops it...
    w.db.conn()
        .execute("UPDATE placement SET next_at = 0", [])
        .unwrap();
    std::fs::set_permissions(staging(&w), std::fs::Permissions::from_mode(0o777)).unwrap();
    placer_pass(&w, false).await;
    let p = state(&w, 1);
    assert_eq!(
        (p.state.as_str(), p.stuck_from.as_deref()),
        ("stuck", Some("filled"))
    );
    // ...with no writable mount kept up for it until it is due.
    assert!(!Journal(&w.db).needs_mount(w.now.get()));
    later(&w);
    assert!(Journal(&w.db).needs_mount(w.now.get()));
    placer_pass(&w, false).await;
    assert_eq!(state(&w, 1).state, "stuck", "not fixed yet");
    // Put right: it goes on from `filled`, not from the link.
    std::fs::set_permissions(staging(&w), std::fs::Permissions::from_mode(0o700)).unwrap();
    later(&w);
    placer_pass(&w, false).await;
    assert_eq!(state(&w, 1).state, "done");
    assert_eq!(
        std::fs::read(w.nas.path().join("lts/surveys/a.segy")).unwrap(),
        body
    );
}

#[tokio::test]
async fn a_stuck_placement_the_service_ended_cleans_up() {
    // E5's way out: an Owner cancels it.
    let w = world();
    let body = data(1000);
    give(&w, ticket(1, "lts/surveys", "a.segy", &body, true), &body);
    faults::STOP_AT.with(|s| *s.borrow_mut() = Some("filled".into()));
    pass(&w).await;
    let name = state(&w, 1).staging_name;
    assert!(staging(&w).join(&name).exists());
    std::fs::set_permissions(staging(&w), std::fs::Permissions::from_mode(0o777)).unwrap();
    placer_pass(&w, false).await;
    assert_eq!(state(&w, 1).state, "stuck");
    // The problem is still there, so only the service's answer can end it.
    w.svc
        .state
        .lock()
        .unwrap()
        .ended
        .insert(1, "cancelled".into());
    later(&w);
    placer_pass(&w, false).await;
    assert_eq!(state(&w, 1).state, "failed");
    assert!(!staging(&w).join(&name).exists());
    assert!(!w.nas.path().join("lts/surveys/a.segy").exists());
    assert!(!Journal(&w.db).needs_mount(w.now.get() + STUCK_RETRY_MS * 2));
}

#[tokio::test]
async fn a_restart_at_the_link_takes_every_check_again() {
    // E6: it used to link on an old `go`, with `at_staging.is_some()` as its
    // only test, so a swapped file or a stale approval got a name.
    let w = world();
    let body = data(1000);
    give(&w, ticket(1, "lts/surveys", "a.segy", &body, true), &body);
    faults::STOP_AT.with(|s| *s.borrow_mut() = Some("linking".into()));
    pass(&w).await;
    assert_eq!(state(&w, 1).state, "linking");
    // A staging file swapped while it was down.
    let st = state(&w, 1);
    let f = staging(&w).join(&st.staging_name);
    std::fs::remove_file(&f).unwrap();
    std::fs::write(&f, &body).unwrap();
    std::fs::set_permissions(&f, std::fs::Permissions::from_mode(0o640)).unwrap();
    placer_pass(&w, false).await;
    let p = state(&w, 1);
    assert_eq!(
        (p.state.as_str(), p.reason.as_deref()),
        ("failed", Some("staging_file_changed"))
    );
    assert!(!w.nas.path().join("lts/surveys/a.segy").exists());
    // A `go` the service would now refuse.
    let w = world();
    give(&w, ticket(1, "lts/surveys", "a.segy", &body, true), &body);
    faults::STOP_AT.with(|s| *s.borrow_mut() = Some("linking".into()));
    pass(&w).await;
    w.svc.state.lock().unwrap().refuse_go = Some("access_denied".into());
    placer_pass(&w, false).await;
    assert_eq!(state(&w, 1).reason.as_deref(), Some("access_denied"));
    assert!(!w.nas.path().join("lts/surveys/a.segy").exists());
    assert!(staging_empty(&w));
    assert_eq!(steps(&w).iter().filter(|s| *s == "go").count(), 2);
}

#[tokio::test]
async fn a_crash_right_after_the_link_never_gives_a_second_name() {
    let w = world();
    let body = data(1000);
    give(&w, ticket(1, "lts/surveys", "a.segy", &body, true), &body);
    faults::STOP_AT.with(|s| *s.borrow_mut() = Some("link_taken".into()));
    pass(&w).await;
    assert_eq!(state(&w, 1).state, "linking");
    assert_eq!(
        std::fs::metadata(w.nas.path().join("lts/surveys/a.segy"))
            .unwrap()
            .nlink(),
        2
    );
    placer_pass(&w, false).await;
    assert_eq!(state(&w, 1).state, "done");
    let md = std::fs::metadata(w.nas.path().join("lts/surveys/a.segy")).unwrap();
    assert_eq!(md.nlink(), 1);
    assert!(!w.nas.path().join("lts/surveys/a-restored.segy").exists());
    assert!(staging_empty(&w));
}

#[tokio::test]
async fn a_link_the_server_took_and_answered_exists_isnt_a_taken_name() {
    // Minor, EEXIST on a replayed link: the next name would have made two.
    let w = world();
    let body = data(1000);
    give(&w, ticket(1, "lts/surveys", "a.segy", &body, true), &body);
    faults::LINK_REPLAYED.with(|f| f.set(true));
    pass(&w).await;
    let p = state(&w, 1);
    assert_eq!(
        (p.state.as_str(), p.outcome.as_deref()),
        ("done", Some("placed"))
    );
    assert!(!w.nas.path().join("lts/surveys/a-restored.segy").exists());
    assert_eq!(
        std::fs::metadata(w.nas.path().join("lts/surveys/a.segy"))
            .unwrap()
            .nlink(),
        1
    );
}

#[tokio::test]
async fn a_crash_making_the_placeholder_leaves_no_orphan_at_the_name() {
    // Minor, the placeholder orphan: the file took `-restored`, and the
    // placeholder stayed under the user's name.
    let w = world();
    let body = data(1000);
    give(&w, ticket(1, "lts/surveys", "a.segy", &body, true), &body);
    faults::LINK_EACCES.with(|f| f.set(true));
    faults::STOP_AT.with(|s| *s.borrow_mut() = Some("placeholder_made".into()));
    pass(&w).await;
    assert_eq!(state(&w, 1).state, "placeholder_make");
    placer_pass(&w, false).await;
    faults::LINK_EACCES.with(|f| f.set(false));
    assert_eq!(state(&w, 1).state, "done");
    assert_eq!(
        std::fs::read(w.nas.path().join("lts/surveys/a.segy")).unwrap(),
        body
    );
    assert!(!w.nas.path().join("lts/surveys/a-restored.segy").exists());
    // A user's file there instead is never taken for it.
    let w = world();
    give(&w, ticket(1, "lts/surveys", "a.segy", &body, true), &body);
    faults::LINK_EACCES.with(|f| f.set(true));
    faults::STOP_AT.with(|s| *s.borrow_mut() = Some("placeholder_made".into()));
    pass(&w).await;
    let name = w.nas.path().join("lts/surveys/a.segy");
    std::fs::remove_file(&name).unwrap();
    std::fs::write(&name, b"a user's").unwrap();
    placer_pass(&w, false).await;
    faults::LINK_EACCES.with(|f| f.set(false));
    assert_eq!(std::fs::read(&name).unwrap(), b"a user's");
    assert_eq!(
        std::fs::read(w.nas.path().join("lts/surveys/a-restored.segy")).unwrap(),
        body
    );
}

#[tokio::test]
async fn a_read_only_file_is_placed() {
    // E7: every reopen was O_RDWR, so a 0444 source failed after P3. Root isn't
    // stopped by a mode, so the fault refuses the opens for writing whoever runs
    // this, from the point the file is filled.
    let w = world();
    let body = data(1000);
    let mut t = ticket(1, "lts/surveys", "a.segy", &body, true);
    t["acl"]["mode"] = json!(0o440);
    give(&w, t, &body);
    // With its descriptor on already, as after a crash there.
    faults::STOP_AT.with(|s| *s.borrow_mut() = Some("acl_set".into()));
    pass(&w).await;
    faults::WRITE_OPENS_REFUSED.with(|f| f.set(true));
    placer_pass(&w, false).await;
    faults::WRITE_OPENS_REFUSED.with(|f| f.set(false));
    let p = state(&w, 1);
    assert_eq!(p.state, "done", "{p:?}");
    assert_eq!(p.owner_restored, Some(true));
    let md = std::fs::metadata(w.nas.path().join("lts/surveys/a.segy")).unwrap();
    assert_eq!(md.mode() & 0o7777, 0o440);
}

#[tokio::test]
async fn trouble_after_the_link_never_fails_the_placement() {
    // E7: `failed` while the file sat under its name.
    let w = world();
    let body = data(1000);
    give(&w, ticket(1, "lts/surveys", "a.segy", &body, true), &body);
    faults::P5_REFUSED.with(|f| f.set(true));
    pass(&w).await;
    faults::P5_REFUSED.with(|f| f.set(false));
    let p = state(&w, 1);
    assert_eq!(p.state, "done", "{p:?}");
    assert_eq!(p.owner_restored, Some(false));
    assert!(w.nas.path().join("lts/surveys/a.segy").exists());
}

#[tokio::test]
async fn setuid_waits_for_the_owner() {
    // E8: a 4755 file sat setuid-root from P4 to P5, longer after a crash.
    let w = world();
    let body = data(1000);
    let mut t = ticket(1, "lts/surveys", "a.segy", &body, true);
    t["acl"]["mode"] = json!(0o4755);
    give(&w, t, &body);
    faults::STOP_AT.with(|s| *s.borrow_mut() = Some("linked".into()));
    pass(&w).await;
    let f = w.nas.path().join("lts/surveys/a.segy");
    assert_eq!(std::fs::metadata(&f).unwrap().mode() & 0o7777, 0o755);
    placer_pass(&w, false).await;
    assert_eq!(state(&w, 1).state, "done");
    assert_eq!(std::fs::metadata(&f).unwrap().mode() & 0o7777, 0o4755);
}

#[tokio::test]
async fn a_second_annex_on_the_export_has_its_own_staging_folder() {
    // E9: the second one met a folder its journal didn't make, stuck for good.
    let w = world();
    let body = data(1000);
    give(&w, ticket(1, "lts/surveys", "a.segy", &body, true), &body);
    pass(&w).await;
    assert_eq!(state(&w, 1).state, "done");
    let db2 = AnnexDb::open_in_memory().unwrap();
    let pl = open_root(w.nas.path(), "").unwrap();
    let ro = open_root(w.nas.path(), "").unwrap();
    let t = ticket(2, "lts/surveys", "b.segy", &body, true);
    {
        let mut s = w.svc.state.lock().unwrap();
        s.placement_tickets.push(t);
    }
    take_tickets(&w.svc, &db2, 8, 1_000).await;
    let p = Placer {
        svc: &w.svc,
        db: &db2,
        pl: &pl,
        ro: &ro,
        annex_id: 8,
        annex_name: "other",
        annex_rel: "lts",
        flavour: Flavour::Nfs3,
        ids: &w.ids,
        placement_sid: None,
        read_sid: None,
        read_who: None,
        now: 1_000,
        state_dir: w.state.path(),
        accounts_problem: None,
        journal_failed: std::cell::Cell::new(false),
        grace_ms: GRACE_MS,
        smb: false,
    };
    p.pass().await;
    let got = Journal(&db2).get(2).unwrap().unwrap();
    assert_eq!(got.state, "done", "{got:?}");
    assert!(
        staging_folders(&w)
            .iter()
            .any(|n| n.starts_with(".omnifile-staging-8-"))
    );
    assert!(is_staging_dir(&staging_dir(8, "ab12")));
    assert!(!is_staging_dir(".omnifile-staging-"));
    assert!(!is_staging_dir(".omnifile-staging-8"));
    assert!(!is_staging_dir(".omnifile-staging-8-"));
    assert!(!is_staging_dir(".omnifile-staging-8x-ab12"));
    assert!(!is_staging_dir(".omnifile-staging-8-xyz"));
}

#[tokio::test]
async fn a_new_folder_lets_in_whoever_its_parent_does() {
    // E12: mkdir 0700 let only the placement account in, so `go` said no for
    // any new restore subfolder.
    let w = world();
    let body = data(1000);
    give(&w, ticket(1, "restore/x/y", "a.segy", &body, false), &body);
    pass(&w).await;
    assert_eq!(state(&w, 1).state, "done", "{:?}", state(&w, 1));
    for d in ["restore/x", "restore/x/y"] {
        let md = std::fs::metadata(w.nas.path().join(d)).unwrap();
        assert_eq!(md.mode() & 0o7777, 0o775, "{d}");
    }
    // A parent the requester can't add to: `go` reads that too.
    let w = world();
    std::fs::set_permissions(
        w.nas.path().join("restore"),
        std::fs::Permissions::from_mode(0o755),
    )
    .unwrap();
    give(&w, ticket(1, "restore/x", "a.segy", &body, false), &body);
    pass(&w).await;
    assert_eq!(state(&w, 1).reason.as_deref(), Some("access_denied"));
    assert!(!w.nas.path().join("restore/x").exists());
}

#[tokio::test]
async fn what_a_failure_couldnt_clean_is_tried_again_and_reported() {
    // E13: errors were swallowed, `failed` was final, and the staged file leaked.
    let w = world();
    let body = data(1000);
    give(&w, ticket(1, "lts/surveys", "a.segy", &body, true), &body);
    w.svc.state.lock().unwrap().refuse_go = Some("acl_mismatch".into());
    faults::CLEANUP_FAILS.with(|f| f.set(true));
    pass(&w).await;
    let p = state(&w, 1);
    assert_eq!(p.state, "failed");
    assert!(p.leftover.is_some());
    assert!(!staging_empty(&w));
    later(&w);
    assert!(Journal(&w.db).needs_mount(w.now.get()));
    placer_pass(&w, false).await;
    assert!(steps(&w).iter().any(|s| s == "leftover"), "reported");
    assert!(state(&w, 1).leftover.is_some());
    faults::CLEANUP_FAILS.with(|f| f.set(false));
    later(&w);
    placer_pass(&w, false).await;
    assert!(state(&w, 1).leftover.is_none());
    assert!(staging_empty(&w));
    assert!(!Journal(&w.db).needs_mount(w.now.get() + STUCK_RETRY_MS * 2));
}

fn removal_with_pointer(w: &World, path: &str, ino: u64, sha: &str) {
    w.db.conn()
        .execute(
            "INSERT INTO removal (removal_id, path, file_id, version_id, rule, ino, size, \
             mtime_ns, hidden_name, state, updated_at) \
             VALUES (1, ?1, 9, 3, 'r', 1, 1, 1, 'h', 'done', 1)",
            [path],
        )
        .unwrap();
    w.db.set_removal_pointer_file(1, ino, sha).unwrap();
}

#[tokio::test]
async fn the_pointer_w4_recorded_goes_by_its_inode_and_hash() {
    // Tests that passed against a bug: the text fallback took every case, so
    // the recorded branch never ran. This text is no pointer's, so only the
    // record can say it is ours.
    let w = world();
    let body = data(1000);
    let ptr = w.nas.path().join("lts/surveys/a.segy.omnifile.txt");
    std::fs::write(&ptr, "a text the fallback would never take").unwrap();
    let ino = std::fs::metadata(&ptr).unwrap().ino();
    removal_with_pointer(
        &w,
        "surveys/a.segy",
        ino,
        &sha_hex(b"a text the fallback would never take"),
    );
    give(&w, ticket(1, "lts/surveys", "a.segy", &body, true), &body);
    pass(&w).await;
    assert_eq!(state(&w, 1).pointer_deleted, Some(true));
    assert!(!ptr.exists());
    // With a record, the exact text isn't enough: another inode stays.
    let w = world();
    let ptr = w.nas.path().join("lts/surveys/a.segy.omnifile.txt");
    let text =
        crate::annex::remover::pointer_text("lts", "surveys/a.segy", 9, "2026-10-01T00:00:00Z");
    std::fs::write(&ptr, &text).unwrap();
    let ino = std::fs::metadata(&ptr).unwrap().ino();
    removal_with_pointer(&w, "surveys/a.segy", ino + 1, &sha_hex(text.as_bytes()));
    give(&w, ticket(1, "lts/surveys", "a.segy", &body, true), &body);
    pass(&w).await;
    assert_eq!(state(&w, 1).pointer_deleted, Some(false));
    assert_eq!(std::fs::read_to_string(&ptr).unwrap(), text);
}

#[tokio::test]
async fn a_pointer_swapped_after_the_look_is_put_back() {
    // Minor, the P6 swap: stat, read and unlink by name would delete the file a
    // user put there in the window.
    let w = world();
    let body = data(1000);
    let ptr = w.nas.path().join("lts/surveys/a.segy.omnifile.txt");
    let text =
        crate::annex::remover::pointer_text("lts", "surveys/a.segy", 9, "2026-10-01T00:00:00Z");
    std::fs::write(&ptr, &text).unwrap();
    give(&w, ticket(1, "lts/surveys", "a.segy", &body, true), &body);
    let p2 = ptr.clone();
    faults::BEFORE_P6.with(|h| {
        *h.borrow_mut() = Some(Box::new(move || {
            std::fs::remove_file(&p2).unwrap();
            std::fs::write(&p2, b"a user's note").unwrap();
        }))
    });
    pass(&w).await;
    assert_eq!(state(&w, 1).state, "done");
    assert_eq!(state(&w, 1).pointer_deleted, Some(false));
    assert_eq!(std::fs::read(&ptr).unwrap(), b"a user's note");
    assert!(staging_empty(&w));
}

#[tokio::test]
async fn a_second_try_after_a_bad_read_back_makes_its_folders_again() {
    // Minor, the CRC retry's folders: the cleanup removed P0's, then skipped P0.
    let w = world();
    let body = data(1000);
    give(&w, ticket(1, "restore/x/y", "a.segy", &body, false), &body);
    faults::CRC_FLIP.with(|f| f.set(true));
    pass(&w).await;
    let p = state(&w, 1);
    assert_eq!(p.state, "done", "{p:?}");
    assert_eq!(
        std::fs::read(w.nas.path().join("restore/x/y/a.segy")).unwrap(),
        body
    );
}

fn last_done(w: &World) -> Value {
    let s = w.svc.state.lock().unwrap();
    s.placement_steps
        .iter()
        .rfind(|(_, b)| b["step"] == "done")
        .unwrap()
        .1
        .clone()
}

#[tokio::test]
async fn done_says_what_p5_saw_and_only_if_it_is_the_copy() {
    // S3: `done` used to stat by name whenever it was sent.
    let w = world();
    let body = data(1000);
    give(&w, ticket(1, "lts/surveys", "a.segy", &body, true), &body);
    faults::STOP_AT.with(|s| *s.borrow_mut() = Some("owned".into()));
    pass(&w).await;
    let f = w.nas.path().join("lts/surveys/a.segy");
    assert_eq!(state(&w, 1).state, "owned");
    let at_p5 = std::fs::metadata(&f).unwrap();
    // A user's edit after P5, before `done` could be sent.
    std::fs::set_permissions(&f, std::fs::Permissions::from_mode(0o640)).unwrap();
    std::fs::write(&f, b"edited").unwrap();
    placer_pass(&w, false).await;
    assert_eq!(last_done(&w)["attrs"]["size"], at_p5.size());
    // An edit before P5: it isn't the copy, and `done` says nothing of it.
    let w = world();
    let ptr = w.nas.path().join("lts/surveys/a.segy.omnifile.txt");
    let text =
        crate::annex::remover::pointer_text("lts", "surveys/a.segy", 9, "2026-10-01T00:00:00Z");
    std::fs::write(&ptr, &text).unwrap();
    give(&w, ticket(1, "lts/surveys", "a.segy", &body, true), &body);
    faults::STOP_AT.with(|s| *s.borrow_mut() = Some("linked".into()));
    pass(&w).await;
    let f = w.nas.path().join("lts/surveys/a.segy");
    std::fs::set_permissions(&f, std::fs::Permissions::from_mode(0o640)).unwrap();
    std::fs::write(&f, b"edited").unwrap();
    placer_pass(&w, false).await;
    let done = last_done(&w);
    assert!(done["attrs"].is_null(), "{done}");
    // And the pointer, a real one of ours, isn't touched over a file that isn't.
    assert_eq!(state(&w, 1).pointer_deleted, Some(false));
    assert_eq!(std::fs::read_to_string(&ptr).unwrap(), text);
}

#[tokio::test]
async fn an_older_version_at_its_origin_takes_a_restored_name_and_leaves_the_pointer() {
    // S6's edge: `names[0]` isn't the file's own name then.
    let w = world();
    let body = data(1000);
    let ptr = w.nas.path().join("lts/surveys/a.segy.omnifile.txt");
    let text =
        crate::annex::remover::pointer_text("lts", "surveys/a.segy", 9, "2026-10-01T00:00:00Z");
    std::fs::write(&ptr, &text).unwrap();
    let mut t = ticket(1, "lts/surveys", "a.segy", &body, true);
    t["name"] = json!("a.segy");
    t["names"] = json!(["a-restored.segy", "a-restored-2026-11-05-120000Z.segy"]);
    give(&w, t, &body);
    pass(&w).await;
    let p = state(&w, 1);
    assert_eq!(
        (p.state.as_str(), p.outcome.as_deref()),
        ("done", Some("restored_name"))
    );
    assert!(ptr.exists(), "the file isn't back under its name");
    assert!(!w.nas.path().join("lts/surveys/a.segy").exists());
}

#[tokio::test]
async fn a_placed_files_age_counts_from_its_placement() {
    // S2: its old dates came back with it.
    let w = world();
    let body = data(1000);
    give(&w, ticket(1, "lts/surveys", "a.segy", &body, true), &body);
    w.now.set(5_000);
    pass(&w).await;
    assert_eq!(placed_at(&w.db, "surveys/a.segy"), Some(5_000));
    assert_eq!(placed_at(&w.db, "surveys/other.segy"), None);
}

// --- the second review -------------------------------------------------------

/// To a setuid script of a user's, between the look and the open.
fn swap_for_a_users_script(f: std::path::PathBuf) {
    std::fs::remove_file(&f).unwrap();
    std::fs::write(&f, b"#!/bin/sh\nid\n").unwrap();
    std::fs::set_permissions(&f, std::fs::Permissions::from_mode(0o755)).unwrap();
}

#[tokio::test]
async fn p5_sets_nothing_on_a_file_that_isnt_what_was_secured() {
    // F1: stat by name, open by name, then chown and restore setuid on the fd.
    let body = data(1000);
    for at in ["between the look and the open", "after a crash at linked"] {
        let w = world();
        let mut t = ticket(1, "lts/surveys", "a.segy", &body, true);
        t["acl"]["mode"] = json!(0o4775);
        give(&w, t, &body);
        let f = w.nas.path().join("lts/surveys/a.segy");
        faults::P5_OWNER_SETS.with(|x| x.set(0));
        if at.starts_with("between") {
            let g = f.clone();
            faults::BEFORE_P5_OPEN
                .with(|h| *h.borrow_mut() = Some(Box::new(move || swap_for_a_users_script(g))));
            pass(&w).await;
        } else {
            // In place: the same inode, other bytes.
            faults::STOP_AT.with(|s| *s.borrow_mut() = Some("linked".into()));
            pass(&w).await;
            std::fs::set_permissions(&f, std::fs::Permissions::from_mode(0o775)).unwrap();
            std::fs::write(&f, b"#!/bin/sh\nid\n").unwrap();
            placer_pass(&w, false).await;
        }
        let p = state(&w, 1);
        assert_eq!(p.state, "done", "{at}: {p:?}");
        assert_eq!(p.owner_restored, Some(false), "{at}");
        assert_eq!(faults::P5_OWNER_SETS.with(|x| x.get()), 0, "{at}: no chown");
        assert_eq!(
            std::fs::metadata(&f).unwrap().mode() & 0o6000,
            0,
            "{at}: never setuid"
        );
        assert!(last_done(&w)["attrs"].is_null(), "{at}");
    }
    // Same inode and size, an edit that only the mtime gives away.
    let w = world();
    let mut t = ticket(1, "lts/surveys", "a.segy", &body, true);
    t["acl"]["mode"] = json!(0o4775);
    give(&w, t, &body);
    let f = w.nas.path().join("lts/surveys/a.segy");
    faults::P5_OWNER_SETS.with(|x| x.set(0));
    faults::STOP_AT.with(|s| *s.borrow_mut() = Some("linked".into()));
    pass(&w).await;
    std::fs::set_permissions(&f, std::fs::Permissions::from_mode(0o775)).unwrap();
    let mut other = body.clone();
    other[0] ^= 0xff;
    std::fs::write(&f, &other).unwrap();
    let was = filetime_of(&f);
    set_mtime(&f, was + 7);
    placer_pass(&w, false).await;
    assert_eq!(faults::P5_OWNER_SETS.with(|x| x.get()), 0, "same size");
    assert_eq!(std::fs::metadata(&f).unwrap().mode() & 0o6000, 0);
    // An edit after the chown, before the setuid comes back.
    let w = world();
    let mut t = ticket(1, "lts/surveys", "a.segy", &body, true);
    t["acl"]["mode"] = json!(0o4775);
    give(&w, t, &body);
    let f = w.nas.path().join("lts/surveys/a.segy");
    let g = f.clone();
    faults::BEFORE_P5_RESTORE.with(|h| {
        *h.borrow_mut() = Some(Box::new(move || {
            std::fs::write(&g, b"#!/bin/sh\nid\n").unwrap();
        }))
    });
    pass(&w).await;
    assert_eq!(std::fs::metadata(&f).unwrap().mode() & 0o6000, 0);
}

fn filetime_of(f: &std::path::Path) -> i64 {
    std::fs::metadata(f).unwrap().mtime()
}

fn set_mtime(f: &std::path::Path, secs: i64) {
    let t = std::time::UNIX_EPOCH + std::time::Duration::from_secs(secs as u64);
    std::fs::File::options()
        .write(true)
        .open(f)
        .unwrap()
        .set_modified(t)
        .unwrap();
}

#[tokio::test]
async fn a_placeholder_found_on_resume_still_goes_through_go() {
    // F3: a resume at placeholder_make skipped `go` and the statx.
    let w = world();
    let body = data(1000);
    give(&w, ticket(1, "lts/surveys", "a.segy", &body, true), &body);
    faults::LINK_EACCES.with(|f| f.set(true));
    faults::STOP_AT.with(|s| *s.borrow_mut() = Some("placeholder_made".into()));
    pass(&w).await;
    assert_eq!(state(&w, 1).state, "placeholder_make");
    w.svc.state.lock().unwrap().refuse_go = Some("acl_mismatch".into());
    placer_pass(&w, false).await;
    faults::LINK_EACCES.with(|f| f.set(false));
    let p = state(&w, 1);
    assert_eq!(p.state, "failed", "{p:?}");
    assert!(!w.nas.path().join("lts/surveys/a.segy").exists());
    // A swapped staging file doesn't get the name either.
    let w = world();
    give(&w, ticket(1, "lts/surveys", "a.segy", &body, true), &body);
    faults::LINK_EACCES.with(|f| f.set(true));
    faults::STOP_AT.with(|s| *s.borrow_mut() = Some("placeholder_made".into()));
    pass(&w).await;
    let stg = staging(&w).join(state(&w, 1).staging_name);
    std::fs::remove_file(&stg).unwrap();
    std::fs::write(&stg, b"a user's").unwrap();
    placer_pass(&w, false).await;
    faults::LINK_EACCES.with(|f| f.set(false));
    assert_ne!(
        std::fs::read(w.nas.path().join("lts/surveys/a.segy")).unwrap_or_default(),
        b"a user's"
    );
}

/// Passes until the bound on tries has run out.
async fn tries_run_out(w: &World) {
    for _ in 0..MAX_ATTEMPTS + 2 {
        later(w);
        placer_pass(w, false).await;
    }
}

fn stuck_at(w: &World, pid: u64, from: &str) -> String {
    let p = state(w, pid);
    assert_eq!(
        (p.state.as_str(), p.stuck_from.as_deref()),
        ("stuck", Some(from)),
        "{p:?}"
    );
    p.reason.unwrap_or_default()
}

fn cancel(w: &World, pid: u64) {
    w.svc
        .state
        .lock()
        .unwrap()
        .ended
        .insert(pid, "cancelled".into());
}

#[tokio::test]
async fn an_absent_service_never_costs_a_staged_copy() {
    // F5: a 620 s outage at `go` gave `gave_up`, and the full file was deleted.
    // Round 3: no silent forever either. It sticks, and keeps the bytes.
    let w = world();
    let body = data(1000);
    give(&w, ticket(1, "lts/surveys", "a.segy", &body, true), &body);
    faults::STOP_AT.with(|s| *s.borrow_mut() = Some("secured".into()));
    pass(&w).await;
    let name = state(&w, 1).staging_name;
    w.svc.state.lock().unwrap().down = true;
    tries_run_out(&w).await;
    assert!(stuck_at(&w, 1, "secured").starts_with("trouble: go"));
    assert!(staging(&w).join(&name).exists());
    // No writable mount kept up for it until it is due.
    assert!(!Journal(&w.db).needs_mount(w.now.get()));
    w.svc.state.lock().unwrap().down = false;
    later(&w);
    placer_pass(&w, false).await;
    assert_eq!(state(&w, 1).state, "done");
}

#[tokio::test]
async fn trouble_that_lasts_is_stuck_and_keeps_the_bytes_until_a_cancel_is_heard() {
    // Edge B1 and S4, by class: each of these retried for ever, or gave up and
    // deleted what it had, however much that was.
    #[derive(Clone, Copy, Debug)]
    enum Trouble {
        Blob503,
        NasAwayMidFill,
        ReadBack,
        ServiceRefusesGo,
    }
    use Trouble::*;
    for (trouble, stop, from) in [
        (Blob503, None, "filling"),
        (NasAwayMidFill, None, "filling"),
        (ReadBack, Some("filled"), "filled"),
        (ServiceRefusesGo, Some("secured"), "secured"),
    ] {
        let w = world();
        let body = data(1000);
        give(&w, ticket(1, "lts/surveys", "a.segy", &body, true), &body);
        let on = |v: bool| match trouble {
            Blob503 => w.svc.state.lock().unwrap().read_status = v.then_some(503),
            NasAwayMidFill => faults::FILL_EIO.with(|f| f.set(v)),
            ReadBack => faults::READBACK_EIO.with(|f| f.set(v)),
            ServiceRefusesGo => w.svc.state.lock().unwrap().refuse_steps = v,
        };
        if let Some(at) = stop {
            faults::STOP_AT.with(|s| *s.borrow_mut() = Some(at.into()));
            pass(&w).await;
            on(true);
        } else {
            on(true);
            pass(&w).await;
        }
        let name = state(&w, 1).staging_name;
        tries_run_out(&w).await;
        assert!(
            stuck_at(&w, 1, from).starts_with("trouble: "),
            "{trouble:?}"
        );
        assert!(staging(&w).join(&name).exists(), "{trouble:?}: kept");
        // It said so.
        if !matches!(trouble, ServiceRefusesGo) {
            assert!(steps(&w).iter().any(|s| s == "stuck"), "{trouble:?}");
        }
        // Only the service's answer ends it.
        on(false);
        cancel(&w, 1);
        later(&w);
        placer_pass(&w, false).await;
        assert_eq!(state(&w, 1).state, "failed", "{trouble:?}");
        assert!(!staging(&w).join(&name).exists(), "{trouble:?}: cleaned");
        assert!(!w.nas.path().join("lts/surveys/a.segy").exists());
    }
}

#[tokio::test]
async fn a_trouble_that_clears_goes_on_from_its_step() {
    let w = world();
    let body = data(1000);
    give(&w, ticket(1, "lts/surveys", "a.segy", &body, true), &body);
    w.svc.state.lock().unwrap().read_status = Some(500);
    pass(&w).await;
    tries_run_out(&w).await;
    stuck_at(&w, 1, "filling");
    w.svc.state.lock().unwrap().read_status = None;
    later(&w);
    placer_pass(&w, false).await;
    assert_eq!(state(&w, 1).state, "done");
    assert_eq!(
        std::fs::read(w.nas.path().join("lts/surveys/a.segy")).unwrap(),
        body
    );
}

#[tokio::test]
async fn back_from_stuck_the_same_trouble_is_stuck_again_at_once() {
    // An hourly try, not another ten minutes of backing off with the mount up.
    let w = world();
    let body = data(1000);
    give(&w, ticket(1, "lts/surveys", "a.segy", &body, true), &body);
    w.svc.state.lock().unwrap().read_status = Some(503);
    pass(&w).await;
    tries_run_out(&w).await;
    stuck_at(&w, 1, "filling");
    later(&w);
    placer_pass(&w, false).await;
    stuck_at(&w, 1, "filling");
    assert!(state(&w, 1).next_at >= w.now.get() + STUCK_RETRY_MS);
}

#[test]
fn a_blob_error_is_told_by_its_status() {
    use crate::annex::service::{READ_DEFINITE, READ_INTEGRITY, READ_REFUSED, read_error};
    use omnifile_blob::BlobError;
    let http = |status| {
        read_error(&BlobError::Http {
            op: "get",
            status,
            code: None,
            message: "x".into(),
            request_id: None,
            retry_after: None,
        })
    };
    for s in [401, 403] {
        assert_eq!(
            (http(s).unavailable, http(s).code.as_deref()),
            (false, Some(READ_REFUSED)),
            "{s}"
        );
    }
    for s in [404, 409, 412, 416] {
        assert_eq!(
            (http(s).unavailable, http(s).code.as_deref()),
            (false, Some(READ_DEFINITE)),
            "{s}"
        );
    }
    for s in [408, 429, 500, 502, 503, 504] {
        assert_eq!((http(s).unavailable, http(s).code), (true, None), "{s}");
    }
    let e = read_error(&BlobError::Integrity("crc".into()));
    assert_eq!(
        (e.unavailable, e.code.as_deref()),
        (false, Some(READ_INTEGRITY))
    );
    let e = read_error(&BlobError::Io(std::io::Error::from_raw_os_error(libc::EIO)));
    assert_eq!((e.unavailable, e.code), (true, None));
}

#[tokio::test]
async fn a_blob_that_says_no_for_good_ends_the_placement() {
    const URL2: &str = "https://sim/blob/1?sig=new";
    for status in [404, 409] {
        let w = world();
        let body = data(1000);
        give(&w, ticket(1, "lts/surveys", "a.segy", &body, true), &body);
        {
            let mut s = w.svc.state.lock().unwrap();
            s.read_status = Some(status);
            s.renewed_read = Some((URL2.into(), "2099-01-01T00:00:00.000Z".into()));
        }
        pass(&w).await;
        let p = state(&w, 1);
        assert_eq!(
            (p.state.as_str(), p.reason.as_deref()),
            ("failed", Some("blob_unreadable")),
            "{status}"
        );
        assert!(staging_empty(&w));
    }
}

#[tokio::test]
async fn a_404_mid_fill_renews_and_reads_once_before_it_counts() {
    // E-3: an archive-date move or an expired rehydrated copy gives this 404, with
    // the data safe in Azure and bytes already staged.
    const URL2: &str = "https://sim/blob/1?sig=new";
    for status in [404, 409] {
        let w = world();
        let body = data(1000);
        give(&w, ticket(1, "lts/surveys", "a.segy", &body, true), &body);
        {
            let mut s = w.svc.state.lock().unwrap();
            s.read_blobs.insert(URL2.into(), body.clone());
            s.url_status.insert(URL.into(), status);
            s.renewed_read = Some((URL2.into(), "2099-01-01T00:00:00.000Z".into()));
        }
        pass(&w).await;
        assert_eq!(state(&w, 1).state, "done", "{status}");
        assert_eq!(
            std::fs::read(w.nas.path().join("lts/surveys/a.segy")).unwrap(),
            body
        );
    }
    // No new credential to be had: not definite yet, and nothing is deleted.
    let w = world();
    let body = data(1000);
    give(&w, ticket(1, "lts/surveys", "a.segy", &body, true), &body);
    w.svc.state.lock().unwrap().read_status = Some(404);
    pass(&w).await;
    let p = state(&w, 1);
    assert_eq!(p.state, "filling", "{p:?}");
}

#[tokio::test]
async fn a_403_renews_as_live_answers_it_and_a_range_that_fails_its_crc_is_fetched_again() {
    // Live marked every blob error `unavailable`, so the renew never ran there.
    const URL2: &str = "https://sim/blob/1?sig=new";
    let w = world();
    let body = data(1000);
    give(&w, ticket(1, "lts/surveys", "a.segy", &body, true), &body);
    {
        let mut s = w.svc.state.lock().unwrap();
        s.read_blobs.insert(URL2.into(), body.clone());
        s.expired_urls.push(URL.into());
        s.renewed_read = Some((URL2.into(), "2099-01-01T00:00:00.000Z".into()));
    }
    pass(&w).await;
    assert_eq!(state(&w, 1).state, "done");
    let w = world();
    give(&w, ticket(1, "lts/surveys", "a.segy", &body, true), &body);
    w.svc.state.lock().unwrap().read_integrity = 1;
    pass(&w).await;
    assert_eq!(state(&w, 1).state, "filling");
    later(&w);
    placer_pass(&w, false).await;
    assert_eq!(state(&w, 1).state, "done");
}

#[tokio::test]
async fn a_target_folder_gone_at_go_is_target_missing() {
    let w = world();
    let body = data(1000);
    give(&w, ticket(1, "restore/x", "a.segy", &body, false), &body);
    faults::STOP_AT.with(|s| *s.borrow_mut() = Some("secured".into()));
    pass(&w).await;
    std::fs::remove_dir(w.nas.path().join("restore/x")).unwrap();
    later(&w);
    placer_pass(&w, false).await;
    let p = state(&w, 1);
    assert_eq!(
        (p.state.as_str(), p.reason.as_deref()),
        ("failed", Some("target_missing")),
        "{p:?}"
    );
    assert!(!steps(&w).iter().any(|s| s == "go"));
}

#[tokio::test]
async fn a_nas_that_is_away_at_p1_doesnt_fail_the_lock() {
    let w = world();
    let body = data(1000);
    give(&w, ticket(1, "lts/surveys", "a.segy", &body, true), &body);
    faults::LOCK_EIO.with(|f| f.set(true));
    pass(&w).await;
    faults::LOCK_EIO.with(|f| f.set(false));
    assert_eq!(state(&w, 1).state, "create");
    later(&w);
    placer_pass(&w, false).await;
    assert_eq!(state(&w, 1).state, "done");
}

#[tokio::test]
async fn nothing_fails_after_the_link_when_the_nas_is_away_on_resume() {
    // Edge S2: a crash after the staging unlink, then one failed stat of the
    // target: `.ok()` hid it, `staging_gone`, with the file under its name.
    let w = world();
    let body = data(1000);
    give(&w, ticket(1, "lts/surveys", "a.segy", &body, true), &body);
    faults::STOP_AT.with(|s| *s.borrow_mut() = Some("link_taken".into()));
    pass(&w).await;
    let name = state(&w, 1).staging_name;
    std::fs::remove_file(staging(&w).join(&name)).unwrap();
    faults::TARGET_STAT_EIO.with(|f| f.set(1));
    later(&w);
    placer_pass(&w, false).await;
    let p = state(&w, 1);
    assert_eq!(p.state, "linking", "{p:?}");
    later(&w);
    placer_pass(&w, false).await;
    assert_eq!(state(&w, 1).state, "done");
    assert_eq!(
        std::fs::read(w.nas.path().join("lts/surveys/a.segy")).unwrap(),
        body
    );
}

#[tokio::test]
async fn an_eio_at_gos_statx_isnt_a_staging_file_gone() {
    // A whole copy failed after `go` on any error there.
    let w = world();
    let body = data(1000);
    give(&w, ticket(1, "lts/surveys", "a.segy", &body, true), &body);
    faults::GO_STATX_EIO.with(|f| f.set(true));
    pass(&w).await;
    assert_eq!(state(&w, 1).state, "secured");
    later(&w);
    placer_pass(&w, false).await;
    assert_eq!(state(&w, 1).state, "done");
}

#[tokio::test]
async fn a_second_link_left_by_a_failed_unlink_goes_later() {
    // `let _ =`: the staging name stayed as a second link, and the file looked
    // hard-linked to the removal path for good.
    let w = world();
    let body = data(1000);
    give(&w, ticket(1, "lts/surveys", "a.segy", &body, true), &body);
    faults::UNLINK_AFTER_LINK_EIO.with(|f| f.set(true));
    pass(&w).await;
    let p = state(&w, 1);
    assert_eq!(p.state, "done", "{p:?}");
    assert!(p.leftover.is_some());
    let f = w.nas.path().join("lts/surveys/a.segy");
    assert_eq!(std::fs::metadata(&f).unwrap().nlink(), 2);
    later(&w);
    placer_pass(&w, false).await;
    assert_eq!(std::fs::metadata(&f).unwrap().nlink(), 1);
    assert!(state(&w, 1).leftover.is_none());
    assert!(staging_empty(&w));
}

#[tokio::test]
async fn a_pointer_the_nas_cant_show_now_is_tried_again() {
    let w = world();
    let body = data(1000);
    let ptr = w.nas.path().join("lts/surveys/a.segy.omnifile.txt");
    let text =
        crate::annex::remover::pointer_text("lts", "surveys/a.segy", 9, "2026-10-01T00:00:00Z");
    std::fs::write(&ptr, &text).unwrap();
    give(&w, ticket(1, "lts/surveys", "a.segy", &body, true), &body);
    faults::POINTER_EIO.with(|f| f.set(true));
    pass(&w).await;
    assert_eq!(
        state(&w, 1).state,
        "owned",
        "not done with its pointer unknown"
    );
    assert!(ptr.exists());
    later(&w);
    placer_pass(&w, false).await;
    assert_eq!(state(&w, 1).state, "done");
    assert!(!ptr.exists());
    assert_eq!(state(&w, 1).pointer_deleted, Some(true));
}

#[tokio::test]
async fn a_bad_account_name_alerts_and_a_failed_lookup_gets_there_too() {
    // F7's fix logged a warning for ever, with no contact with the service.
    let w = world();
    let body = data(1000);
    give(&w, ticket(1, "lts/surveys", "a.segy", &body, true), &body);
    *w.accounts.borrow_mut() = Some(AccountTrouble::Unknown("nobody has no SID".into()));
    pass(&w).await;
    assert!(
        stuck_at(&w, 1, "ticketed").starts_with("staging_no_account"),
        "at once: an owner fixes the config"
    );
    assert!(steps(&w).iter().any(|s| s == "stuck"));
    assert!(
        staging_folders(&w).is_empty(),
        "nothing is made without them"
    );
    // A cancel is heard.
    cancel(&w, 1);
    later(&w);
    placer_pass(&w, false).await;
    assert_eq!(state(&w, 1).state, "failed");
    // Winbind away: it may clear, so it backs off first.
    let w = world();
    give(&w, ticket(1, "lts/surveys", "a.segy", &body, true), &body);
    *w.accounts.borrow_mut() = Some(AccountTrouble::Lookup("winbind".into()));
    pass(&w).await;
    assert_eq!(state(&w, 1).state, "ticketed");
    assert_eq!(state(&w, 1).attempts, 1);
    tries_run_out(&w).await;
    assert!(stuck_at(&w, 1, "ticketed").starts_with("trouble: "));
    *w.accounts.borrow_mut() = None;
    later(&w);
    placer_pass(&w, false).await;
    assert_eq!(state(&w, 1).state, "done");
}

#[tokio::test]
async fn a_staging_folder_made_never_locked_and_since_removed_is_a_reset() {
    // Edge S1: `staging_made` set, no `staging_ino`, the folder gone: every
    // placement, new ones too, failed on ENOENT for good.
    let w = world();
    let body = data(1000);
    let rel = staging_dir(ANNEX, "ab12");
    std::fs::create_dir(w.nas.path().join(&rel)).unwrap();
    w.db.set_meta("staging_rel", &rel).unwrap();
    w.db.set_meta("staging_made", "1").unwrap();
    give(&w, ticket(1, "lts/surveys", "a.segy", &body, true), &body);
    pass(&w).await;
    assert!(stuck_at(&w, 1, "ticketed").starts_with("staging_unexpected"));
    // The owner looks, and removes it.
    std::fs::remove_dir(w.nas.path().join(&rel)).unwrap();
    later(&w);
    placer_pass(&w, false).await;
    assert_eq!(state(&w, 1).state, "done");
    assert_ne!(w.db.meta("staging_rel").unwrap().unwrap(), rel);
}

#[tokio::test]
async fn a_whole_copy_is_never_given_up_for_a_passing_trouble() {
    // F5's other half: a service that answers badly, for as long as it likes.
    let w = world();
    let body = data(1000);
    give(&w, ticket(1, "lts/surveys", "a.segy", &body, true), &body);
    faults::STOP_AT.with(|s| *s.borrow_mut() = Some("secured".into()));
    pass(&w).await;
    let name = state(&w, 1).staging_name;
    w.svc.state.lock().unwrap().refuse_steps = true;
    tries_run_out(&w).await;
    stuck_at(&w, 1, "secured");
    assert!(staging(&w).join(&name).exists());
    w.svc.state.lock().unwrap().refuse_steps = false;
    later(&w);
    placer_pass(&w, false).await;
    assert_eq!(state(&w, 1).state, "done");
}

#[tokio::test]
async fn progress_in_the_journal_starts_the_count_again() {
    let w = world();
    let body = data(1000);
    give(&w, ticket(1, "lts/surveys", "a.segy", &body, true), &body);
    faults::STOP_AT.with(|s| *s.borrow_mut() = Some("created".into()));
    pass(&w).await;
    w.db.conn()
        .execute("UPDATE placement SET attempts = 3", [])
        .unwrap();
    later(&w);
    faults::STOP_AT.with(|s| *s.borrow_mut() = Some("filled".into()));
    placer_pass(&w, false).await;
    let p = state(&w, 1);
    assert_eq!((p.state.as_str(), p.attempts), ("filled", 0), "{p:?}");
}

#[tokio::test]
async fn a_lookup_that_fails_is_not_a_change_and_not_a_mismatch() {
    // F6: the folder's hash and the file's held winbind-mapped SIDs.
    let w = world();
    let body = data(1000);
    give(&w, ticket(1, "lts/surveys", "a.segy", &body, true), &body);
    faults::STOP_AT.with(|s| *s.borrow_mut() = Some("secured".into()));
    pass(&w).await;
    let acl = state(&w, 1).secured_acl;
    let folder = w.db.meta("staging_acl").unwrap();
    let name = state(&w, 1).staging_name;
    w.ids.1.store(true, Ordering::SeqCst);
    let gos = |w: &World| steps(w).iter().filter(|s| *s == "go").count();
    tries_run_out(&w).await;
    assert!(
        stuck_at(&w, 1, "secured").starts_with("trouble: go: identity"),
        "not staging_changed, not failed, and not silent"
    );
    assert_eq!(
        gos(&w),
        0,
        "`go` isn't asked with a capture that can't be right"
    );
    assert!(staging(&w).join(&name).exists());
    w.ids.1.store(false, Ordering::SeqCst);
    later(&w);
    placer_pass(&w, false).await;
    assert_eq!(state(&w, 1).state, "done");
    // What says "unchanged" holds nothing that went through a lookup.
    assert_eq!(state(&w, 1).secured_acl, acl);
    assert_eq!(w.db.meta("staging_acl").unwrap(), folder);
    let f = std::fs::File::open(staging(&w)).unwrap();
    let st = imp::fstat(&f).unwrap();
    let raw = acl::raw_print(&f, Flavour::Nfs3, &st).unwrap();
    assert_eq!(Some(raw), folder, "the print itself needs no ids");
}

#[test]
fn the_print_changes_with_the_mode_and_the_owner() {
    let d = tempfile::tempdir().unwrap();
    let p = d.path().join("f");
    std::fs::write(&p, b"x").unwrap();
    let f = std::fs::File::open(&p).unwrap();
    let st = imp::fstat(&f).unwrap();
    let a = acl::raw_print(&f, Flavour::Nfs3, &st).unwrap();
    let mut o = st.clone();
    o.uid += 1;
    assert_ne!(a, acl::raw_print(&f, Flavour::Nfs3, &o).unwrap());
    let mut o = st.clone();
    o.gid += 1;
    assert_ne!(a, acl::raw_print(&f, Flavour::Nfs3, &o).unwrap());
    let mut o = st.clone();
    o.mode ^= 0o020;
    assert_ne!(a, acl::raw_print(&f, Flavour::Nfs3, &o).unwrap());
}

#[tokio::test]
async fn a_group_the_server_ignored_stops_at_go() {
    // S8: the literal group the service checks is the real one, or nothing goes.
    let w = world();
    let body = data(1000);
    let mut t = ticket(1, "lts/surveys", "a.segy", &body, true);
    t["gid"] = json!(nix_gid() + 1);
    give(&w, t, &body);
    faults::P3_GROUP_IGNORED.with(|f| f.set(true));
    pass(&w).await;
    faults::P3_GROUP_IGNORED.with(|f| f.set(false));
    let p = state(&w, 1);
    assert_eq!(
        (p.state.as_str(), p.reason.as_deref()),
        ("failed", Some("group_not_set")),
        "{p:?}"
    );
    assert!(!steps(&w).iter().any(|s| s == "go"));
    assert!(!w.nas.path().join("lts/surveys/a.segy").exists());
}

#[tokio::test]
async fn go_carries_the_crc_the_read_back_saw() {
    // N6.
    let w = world();
    let body = data(1000);
    give(&w, ticket(1, "lts/surveys", "a.segy", &body, true), &body);
    faults::STOP_AT.with(|s| *s.borrow_mut() = Some("secured".into()));
    pass(&w).await;
    assert_eq!(
        state(&w, 1).read_crc.as_deref(),
        Some(format!("{:016x}", crc(&body)).as_str())
    );
    w.db.conn()
        .execute("UPDATE placement SET read_crc = '0000000000000000'", [])
        .unwrap();
    later(&w);
    placer_pass(&w, false).await;
    let p = state(&w, 1);
    assert_eq!(p.state, "failed", "{p:?}");
    assert_eq!(p.reason.as_deref(), Some("crc_mismatch"));
    assert!(!w.nas.path().join("lts/surveys/a.segy").exists());
}

#[tokio::test]
async fn a_crash_right_after_p0s_mkdir_is_taken_on_only_if_it_can_only_be_ours() {
    // The minor at P0: a 0700 folder not in `made`, and `go` then refused it for
    // good. (Its name is the target's, so no token can help; `made_pending` is
    // journalled before the mkdir.)
    let w = world();
    let body = data(1000);
    give(&w, ticket(1, "restore/x", "a.segy", &body, false), &body);
    faults::STOP_AT.with(|s| *s.borrow_mut() = Some("p0_made".into()));
    pass(&w).await;
    let folder = w.nas.path().join("restore/x");
    assert_eq!(std::fs::metadata(&folder).unwrap().mode() & 0o777, 0o700);
    assert_eq!(state(&w, 1).made_pending.as_deref(), Some("restore/x"));
    later(&w);
    placer_pass(&w, false).await;
    let p = state(&w, 1);
    assert_eq!(p.state, "done", "{p:?}");
    assert!(p.made.contains("restore/x"));
    assert_eq!(
        std::fs::metadata(&folder).unwrap().mode() & 0o777,
        0o775,
        "its parent's"
    );
    // A user's folder there, open or holding files, isn't ours to take on: a
    // failure later doesn't remove it.
    let w = world();
    give(&w, ticket(1, "restore/x", "a.segy", &body, false), &body);
    faults::STOP_AT.with(|s| *s.borrow_mut() = Some("p0_made".into()));
    pass(&w).await;
    let folder = w.nas.path().join("restore/x");
    std::fs::write(folder.join("mine"), b"a user's").unwrap();
    later(&w);
    placer_pass(&w, false).await;
    let p = state(&w, 1);
    assert!(!p.made.contains("restore/x"), "{p:?}");
}

#[tokio::test]
async fn a_cleanup_that_fails_on_the_crc_retry_doesnt_lose_the_file() {
    // The minor at the read-back: the journal took a new name, and the old file
    // stayed for good.
    let w = world();
    let body = data(1000);
    give(&w, ticket(1, "lts/surveys", "a.segy", &body, true), &body);
    faults::STOP_AT.with(|s| *s.borrow_mut() = Some("filled".into()));
    pass(&w).await;
    let old = state(&w, 1).staging_name;
    faults::CRC_FLIP.with(|f| f.set(true));
    faults::CLEANUP_FAILS.with(|f| f.set(true));
    later(&w);
    placer_pass(&w, false).await;
    faults::CRC_FLIP.with(|f| f.set(false));
    faults::CLEANUP_FAILS.with(|f| f.set(false));
    let p = state(&w, 1);
    assert_eq!(p.staging_name, old, "its name stays in the journal: {p:?}");
    assert!(staging(&w).join(&old).exists());
    later(&w);
    placer_pass(&w, false).await;
    assert_eq!(state(&w, 1).state, "done");
    assert!(staging_empty(&w));
}

#[tokio::test]
async fn a_staging_file_no_placement_knows_is_reported_once_and_kept() {
    // P7's second half, promised and not built.
    let w = world();
    let body = data(1000);
    give(&w, ticket(1, "lts/surveys", "a.segy", &body, true), &body);
    pass(&w).await;
    let stray = staging(&w).join("stray");
    std::fs::write(&stray, b"who knows").unwrap();
    let events = |w: &World| {
        w.db.outbox(100)
            .unwrap()
            .into_iter()
            .filter(|e| e.3["reason"] == "staging_orphan")
            .count()
    };
    placer_pass(&w, false).await;
    assert_eq!(events(&w), 1);
    placer_pass(&w, false).await;
    assert_eq!(events(&w), 1, "once");
    assert_eq!(std::fs::read(&stray).unwrap(), b"who knows");
}

#[tokio::test]
async fn p5_outage_is_stuck_never_failed_and_never_done_with_the_wrong_owner() {
    // Round 2 gave up after ten minutes, and the placement account was the
    // owner for good. Now: tried again, then stuck, and the file stays.
    for fault in ["open", "chown", "lookup"] {
        let w = world();
        let body = data(1000);
        give(&w, ticket(1, "lts/surveys", "a.segy", &body, true), &body);
        faults::STOP_AT.with(|s| *s.borrow_mut() = Some("linked".into()));
        pass(&w).await;
        let on = |v: bool| match fault {
            "open" => faults::P5_EIO.with(|f| f.set(if v { 1000 } else { 0 })),
            "chown" => faults::P5_SET_EIO.with(|f| f.set(v)),
            _ => w.ids.1.store(v, Ordering::SeqCst),
        };
        on(true);
        tries_run_out(&w).await;
        stuck_at(&w, 1, "linked");
        assert!(
            !steps(&w).iter().any(|s| s == "done" || s == "failed"),
            "{fault}"
        );
        assert_eq!(
            std::fs::read(w.nas.path().join("lts/surveys/a.segy")).unwrap(),
            body,
            "{fault}"
        );
        on(false);
        later(&w);
        placer_pass(&w, false).await;
        let p = state(&w, 1);
        assert_eq!(p.state, "done", "{fault}: {p:?}");
        assert_eq!(p.owner_restored, Some(true), "{fault}");
    }
    // A refusal that stays is an answer: after the bound's tries the placement
    // account keeps it, and it ends.
    let w = world();
    let body = data(1000);
    give(&w, ticket(1, "lts/surveys", "a.segy", &body, true), &body);
    faults::P5_SET_REFUSED.with(|f| f.set(true));
    pass(&w).await;
    assert_eq!(state(&w, 1).state, "linked", "not on one try");
    for _ in 1..MAX_ATTEMPTS {
        later(&w);
        placer_pass(&w, false).await;
    }
    faults::P5_SET_REFUSED.with(|f| f.set(false));
    let p = state(&w, 1);
    assert_eq!((p.state.as_str(), p.owner_restored), ("done", Some(false)));
}

#[tokio::test]
async fn a_failure_with_trouble_cleaning_up_keeps_what_was_left() {
    // Tests that passed against a bug: E13's goes through a refused `go`. Here
    // fail()'s result itself, from a full disk.
    let w = world();
    let body = data(1000);
    give(&w, ticket(1, "lts/surveys", "a.segy", &body, true), &body);
    faults::ENOSPC_AT.with(|f| f.set(1));
    faults::CLEANUP_FAILS.with(|f| f.set(true));
    pass(&w).await;
    faults::ENOSPC_AT.with(|f| f.set(0));
    faults::CLEANUP_FAILS.with(|f| f.set(false));
    let p = state(&w, 1);
    assert_eq!(p.state, "failed", "{p:?}");
    assert!(p.leftover.is_some(), "{p:?}");
    later(&w);
    placer_pass(&w, false).await;
    assert!(state(&w, 1).leftover.is_none());
    assert!(staging_empty(&w));
}

#[test]
fn the_lock_allows_group_and_everyone_only_what_gives_no_data_access() {
    // F9. The ACL's shape is the reviewer's memory of knfsd's mask_from_posix;
    // no server has confirmed it.
    use crate::annex::acl::ACE4_ACCESS_ALLOWED as ALLOW;
    use base64::Engine;
    let xdr = |aces: &[(u32, &str, u32)]| {
        let mut b = (aces.len() as u32).to_be_bytes().to_vec();
        for (kind, who, mask) in aces {
            b.extend(kind.to_be_bytes());
            b.extend(0u32.to_be_bytes());
            b.extend(mask.to_be_bytes());
            b.extend((who.len() as u32).to_be_bytes());
            b.extend(who.as_bytes());
            b.extend(vec![0u8; (4 - who.len() % 4) % 4]);
        }
        json!({"kind": "nfs4", "raw": base64::engine::general_purpose::STANDARD.encode(b)})
    };
    let acl = |group: u32, everyone: u32| {
        xdr(&[
            (ALLOW, "OWNER@", 0x001f_01ff),
            (ALLOW, "GROUP@", group),
            (ALLOW, "EVERYONE@", everyone),
        ])
    };
    let locked = |a: &Value| imp::locked(a, Flavour::Nfs4, None, None, None);
    assert_eq!(imp::NFS4_NO_DATA_ACCESS, 0x0012_0080);
    assert!(locked(&acl(0, 0)));
    // READ_ATTRIBUTES | READ_ACL | SYNCHRONIZE: what a server may leave.
    assert!(locked(&acl(0x0012_0080, 0x0012_0080)));
    assert!(locked(&acl(0x0000_0080, 0x0010_0000)));
    for (name, bit) in [
        ("READ_DATA", 0x1u32),
        ("WRITE_DATA", 0x2),
        ("APPEND_DATA", 0x4),
        ("READ_NAMED_ATTRS", 0x8),
        ("WRITE_NAMED_ATTRS", 0x10),
        ("EXECUTE", 0x20),
        ("DELETE_CHILD", 0x40),
        ("WRITE_ATTRIBUTES", 0x100),
        ("DELETE", 0x1_0000),
        ("WRITE_ACL", 0x4_0000),
        ("WRITE_OWNER", 0x8_0000),
    ] {
        assert!(!locked(&acl(0x0012_0080 | bit, 0)), "GROUP@ {name}");
        assert!(!locked(&acl(0, 0x0012_0080 | bit)), "EVERYONE@ {name}");
    }
    // A named principal gets nothing of the kind.
    assert!(!locked(&xdr(&[
        (ALLOW, "OWNER@", 0x001f_01ff),
        (ALLOW, "someone@dom", 0x0000_0080),
    ])));
    // And what a server denies is no matter.
    assert!(locked(&xdr(&[
        (1, "GROUP@", 0x001f_0000),
        (ALLOW, "OWNER@", 0x001f_01ff),
        (ALLOW, "GROUP@", 0x0012_0080),
    ])));
}

#[tokio::test]
async fn a_staging_folder_someone_removed_gets_a_new_one_and_what_was_in_it_fails() {
    let w = world();
    let body = data(1000);
    give(&w, ticket(1, "lts/surveys", "a.segy", &body, true), &body);
    faults::STOP_AT.with(|s| *s.borrow_mut() = Some("filled".into()));
    pass(&w).await;
    let old = w.db.meta("staging_rel").unwrap().unwrap();
    std::fs::remove_dir_all(staging(&w)).unwrap();
    later(&w);
    placer_pass(&w, false).await;
    let p = state(&w, 1);
    assert_eq!(p.state, "failed", "{p:?}");
    assert_eq!(p.reason.as_deref(), Some("staging_gone"));
    give(&w, ticket(2, "lts/surveys", "b.segy", &body, true), &body);
    pass(&w).await;
    assert_eq!(state(&w, 2).state, "done");
    assert_ne!(w.db.meta("staging_rel").unwrap().unwrap(), old);
}

#[tokio::test]
async fn the_capture_for_go_fails_on_a_failed_lookup_and_never_goes_out_with_no_sids() {
    // Edge S3: the capture swallowed failures, so `go` went out with no owner_sid
    // or group_sid, the service answered acl_mismatch, and a whole copy went.
    // With the numbers in the ticket nothing else looks them up.
    let w = world();
    let body = data(1000);
    let mut t = ticket(1, "lts/surveys", "a.segy", &body, true);
    t["uid"] = json!(nix_uid());
    t["gid"] = json!(nix_gid());
    give(&w, t, &body);
    faults::STOP_AT.with(|s| *s.borrow_mut() = Some("secured".into()));
    pass(&w).await;
    w.ids.1.store(true, Ordering::SeqCst);
    tries_run_out(&w).await;
    assert!(stuck_at(&w, 1, "secured").contains("staged ACL"));
    assert!(!steps(&w).iter().any(|s| s == "go"));
    assert!(staging(&w).join(state(&w, 1).staging_name).exists());
    w.ids.1.store(false, Ordering::SeqCst);
    later(&w);
    placer_pass(&w, false).await;
    assert_eq!(state(&w, 1).state, "done");
    let go = w.svc.state.lock().unwrap().placement_steps.clone();
    let go = go.iter().find(|(_, b)| b["step"] == "go").unwrap();
    assert_eq!(go.1["uid"], nix_uid(), "the numbers it speaks of");
    assert_eq!(go.1["gid"], nix_gid());
}

#[tokio::test]
async fn a_lookup_that_fails_isnt_read_as_no_such_group() {
    // With the owner's number in the ticket, only the group is looked up: a
    // failure there is not `group_not_set`.
    let w = world();
    let body = data(1000);
    let mut t = ticket(1, "lts/surveys", "a.segy", &body, true);
    t["uid"] = json!(nix_uid());
    give(&w, t, &body);
    faults::STOP_AT.with(|s| *s.borrow_mut() = Some("secured".into()));
    pass(&w).await;
    w.ids.1.store(true, Ordering::SeqCst);
    later(&w);
    placer_pass(&w, false).await;
    w.ids.1.store(false, Ordering::SeqCst);
    let p = state(&w, 1);
    assert_eq!(p.state, "secured", "{p:?}");
    // And the same at P3, before anything is secured.
    let w = world();
    let mut t = ticket(1, "lts/surveys", "a.segy", &body, true);
    t["uid"] = json!(nix_uid());
    give(&w, t, &body);
    faults::STOP_AT.with(|s| *s.borrow_mut() = Some("filled".into()));
    pass(&w).await;
    w.ids.1.store(true, Ordering::SeqCst);
    later(&w);
    placer_pass(&w, false).await;
    w.ids.1.store(false, Ordering::SeqCst);
    let p = state(&w, 1);
    assert_eq!(p.state, "filled", "not group_unmapped: {p:?}");
    later(&w);
    placer_pass(&w, false).await;
    assert_eq!(state(&w, 1).state, "done");
}

#[tokio::test]
async fn progress_in_a_long_fill_starts_the_count_of_tries_again() {
    // F5: the count reset only at the link, so a long fill could run out of tries.
    let w = world();
    let body = data((3 * FILL_JOURNAL_EVERY) as usize);
    give(&w, ticket(1, "lts/surveys", "a.segy", &body, true), &body);
    faults::STOP_AT.with(|s| *s.borrow_mut() = Some("filling".into()));
    pass(&w).await;
    assert_eq!(state(&w, 1).state, "filling");
    w.db.conn()
        .execute("UPDATE placement SET attempts = 3", [])
        .unwrap();
    faults::STOP_AT.with(|s| *s.borrow_mut() = Some("filling".into()));
    placer_pass(&w, false).await;
    let p = state(&w, 1);
    assert_eq!((p.state.as_str(), p.attempts), ("filling", 0), "{p:?}");
}

// --- tests that passed against bugs, third review ------------------------------

#[tokio::test]
async fn an_acl_print_that_changed_stops_the_link_though_the_ctime_didnt() {
    // Every chmod in a test moves the ctime too, so the print's compare never
    // decided anything.
    let w = world();
    let body = data(1000);
    give(&w, ticket(1, "lts/surveys", "a.segy", &body, true), &body);
    faults::STOP_AT.with(|s| *s.borrow_mut() = Some("secured".into()));
    pass(&w).await;
    faults::PRINT_DIFFERS.with(|f| f.set(true));
    later(&w);
    placer_pass(&w, false).await;
    let p = state(&w, 1);
    assert_eq!(
        (p.state.as_str(), p.reason.as_deref()),
        ("failed", Some("staging_file_changed")),
        "{p:?}"
    );
    assert!(!w.nas.path().join("lts/surveys/a.segy").exists());
}

#[tokio::test]
async fn p0_takes_on_only_the_placement_accounts_own_closed_folder() {
    // "Open or holding files", the earlier test said, and it tried only holding.
    // Asked directly: a pass stops earlier if the journal's uid is another's.
    let w = world();
    let pl = open_root(w.nas.path(), "").unwrap();
    let ro = open_root(w.nas.path(), "").unwrap();
    let p = Placer {
        svc: &w.svc,
        db: &w.db,
        pl: &pl,
        ro: &ro,
        annex_id: ANNEX,
        annex_name: "lts",
        annex_rel: "lts",
        flavour: Flavour::Nfs3,
        ids: &w.ids,
        placement_sid: None,
        read_sid: None,
        read_who: None,
        now: w.now.get(),
        state_dir: w.state.path(),
        accounts_problem: None,
        journal_failed: std::cell::Cell::new(false),
        grace_ms: GRACE_MS,
        smb: false,
    };
    let folder = w.nas.path().join("new");
    std::fs::create_dir(&folder).unwrap();
    let mode = |m| std::fs::set_permissions(&folder, std::fs::Permissions::from_mode(m)).unwrap();
    w.db.set_meta("staging_uid", &nix_uid().to_string())
        .unwrap();
    mode(0o700);
    let st = |p: &Placer<'_, Fake>| p.pl.stat("new", true).unwrap();
    assert!(
        p.is_our_new_folder(0, "new", &st(&p)).unwrap(),
        "as P0 made it"
    );
    w.db.set_meta("staging_uid", "424242").unwrap();
    assert!(
        !p.is_our_new_folder(0, "new", &st(&p)).unwrap(),
        "another account's"
    );
    w.db.set_meta("staging_uid", &nix_uid().to_string())
        .unwrap();
    mode(0o775);
    assert!(!p.is_our_new_folder(0, "new", &st(&p)).unwrap(), "open");
    mode(0o700);
    std::fs::write(folder.join("mine"), b"x").unwrap();
    assert!(
        !p.is_our_new_folder(0, "new", &st(&p)).unwrap(),
        "holding files"
    );
}

#[test]
fn a_folders_owner_is_read_from_its_descriptor() {
    // `sd_owner_is` always true passed every test.
    let sid = |rid: u32| {
        let mut b = vec![1u8, 2, 0, 0, 0, 0, 0, 5];
        b.extend(21u32.to_le_bytes());
        b.extend(rid.to_le_bytes());
        b
    };
    let mut sd = vec![1u8, 0, 0, 0x80];
    sd.extend(20u32.to_le_bytes());
    sd.extend([0u8; 12]);
    sd.extend(sid(1112));
    assert_eq!(imp::sd_owner(&sd).as_deref(), Some("S-1-5-21-1112"));
    assert_eq!(imp::sd_owner(&sd[..24]), None, "cut short");
    assert!(imp::sd_owner_matches(&sd, Some("S-1-5-21-1112")));
    assert!(
        !imp::sd_owner_matches(&sd, Some("S-1-5-21-1113")),
        "someone else's"
    );
    assert!(!imp::sd_owner_matches(&sd, None));
    assert_eq!(imp::sd_owner(&[0u8; 8]), None);
    // A file with no descriptor (no server here) is nobody's.
    let d = tempfile::tempdir().unwrap();
    let f = std::fs::File::open(d.path()).unwrap();
    assert!(!imp::sd_owner_is(&f, Some("S-1-5-21-1112")));
    assert!(!imp::sd_owner_is(&f, None));
}

#[tokio::test]
async fn over_smb_a_new_folder_is_ours_only_by_its_descriptor() {
    let w = world();
    let pl = open_root(w.nas.path(), "").unwrap();
    let ro = open_root(w.nas.path(), "").unwrap();
    let p = Placer {
        svc: &w.svc,
        db: &w.db,
        pl: &pl,
        ro: &ro,
        annex_id: ANNEX,
        annex_name: "lts",
        annex_rel: "lts",
        flavour: Flavour::Smb,
        ids: &w.ids,
        placement_sid: Some("S-1-5-21-1112".into()),
        read_sid: None,
        read_who: None,
        now: w.now.get(),
        state_dir: w.state.path(),
        accounts_problem: None,
        journal_failed: std::cell::Cell::new(false),
        grace_ms: GRACE_MS,
        smb: false,
    };
    // Empty, mode 0700, and no descriptor to say whose.
    std::fs::create_dir(w.nas.path().join("empty")).unwrap();
    std::fs::set_permissions(
        w.nas.path().join("empty"),
        std::fs::Permissions::from_mode(0o700),
    )
    .unwrap();
    let st = pl.stat("empty", true).unwrap();
    assert!(!p.is_our_new_folder(0, "empty", &st).unwrap());
}

#[test]
fn an_inherit_only_allow_counts_in_the_nfs4_lock() {
    // What new files get: the review's undoing, ignoring these, passed every test.
    use crate::annex::acl::ACE4_ACCESS_ALLOWED as ALLOW;
    use base64::Engine;
    let xdr = |aces: &[(u32, u32, &str, u32)]| {
        let mut b = (aces.len() as u32).to_be_bytes().to_vec();
        for (kind, flags, who, mask) in aces {
            b.extend(kind.to_be_bytes());
            b.extend(flags.to_be_bytes());
            b.extend(mask.to_be_bytes());
            b.extend((who.len() as u32).to_be_bytes());
            b.extend(who.as_bytes());
            b.extend(vec![0u8; (4 - who.len() % 4) % 4]);
        }
        json!({"kind": "nfs4", "raw": base64::engine::general_purpose::STANDARD.encode(b)})
    };
    let locked = |a: &Value| imp::locked(a, Flavour::Nfs4, None, None, None);
    let owner = (ALLOW, 0, "OWNER@", 0x001f_01ff);
    assert!(!locked(&xdr(&[
        owner,
        (ALLOW, 0x08, "GROUP@", 0x0012_00a9)
    ])));
    assert!(!locked(&xdr(&[
        owner,
        (ALLOW, 0x08, "EVERYONE@", 0x0000_0001)
    ])));
    assert!(!locked(&xdr(&[
        owner,
        (ALLOW, 0x08, "someone@dom", 0x0012_0080)
    ])));
    assert!(locked(&xdr(&[owner, (ALLOW, 0x08, "GROUP@", 0x0012_0080)])));
}

#[tokio::test]
async fn no_placement_mount_is_a_trouble_like_any_other() {
    // A bad keytab, or a Copy annex with no account, logged a warning for ever,
    // and nothing asked the service whether the placement was still wanted.
    let w = world();
    let body = data(1000);
    give(&w, ticket(1, "lts/surveys", "a.segy", &body, true), &body);
    faults::STOP_AT.with(|s| *s.borrow_mut() = Some("filled".into()));
    pass(&w).await;
    let name = state(&w, 1).staging_name;
    no_mount_pass(&w.svc, &w.db, w.now.get(), "no keytab").await;
    assert_eq!(state(&w, 1).attempts, 1);
    assert!(state(&w, 1).next_at > w.now.get(), "it backs off");
    for _ in 0..MAX_ATTEMPTS {
        later(&w);
        no_mount_pass(&w.svc, &w.db, w.now.get(), "no keytab").await;
    }
    assert!(stuck_at(&w, 1, "filled").contains("no placement mount: no keytab"));
    report_all(&w.svc, &w.db, w.now.get()).await;
    assert!(steps(&w).iter().any(|s| s == "stuck"));
    assert!(staging(&w).join(&name).exists());
    // It goes on asking, and hears of a cancel. A mount cleans up, later.
    later(&w);
    no_mount_pass(&w.svc, &w.db, w.now.get(), "no keytab").await;
    stuck_at(&w, 1, "filled");
    cancel(&w, 1);
    later(&w);
    no_mount_pass(&w.svc, &w.db, w.now.get(), "no keytab").await;
    let p = state(&w, 1);
    assert_eq!(p.state, "failed", "{p:?}");
    assert!(p.leftover.is_some());
    assert!(staging(&w).join(&name).exists(), "no mount, no cleanup");
    later(&w);
    assert!(Journal(&w.db).needs_mount(w.now.get()));
    placer_pass(&w, false).await;
    assert!(state(&w, 1).leftover.is_none());
    assert!(!staging(&w).join(&name).exists());
}

// --- the sweep: only NotFound means absent --------------------------------------

fn eio_at(at: &'static str) {
    faults::EIO_AT.with(|v| v.borrow_mut().push(at));
}

#[tokio::test]
async fn an_outage_at_any_of_these_points_is_tried_again_and_nothing_is_lost() {
    // Each of these read as "isn't there", "not allowed", or nothing at all.
    for (at, folder, origin) in [
        ("p0_parent", "restore/x", false),
        ("p0_stat", "restore/x", false),
        ("inherit", "restore/x", false),
        ("set_acl", "lts/surveys", true),
        ("link_target", "lts/surveys", true),
    ] {
        let w = world();
        let body = data(1000);
        give(&w, ticket(1, folder, "a.segy", &body, origin), &body);
        eio_at(at);
        pass(&w).await;
        let p = state(&w, 1);
        assert_ne!(p.state, "failed", "{at}: {p:?}");
        assert_ne!(p.state, "done", "{at}: it was asked to fail: {p:?}");
        assert_eq!(p.attempts, 1, "{at}");
        later(&w);
        placer_pass(&w, false).await;
        let p = state(&w, 1);
        assert_eq!(p.state, "done", "{at}: {p:?}");
        let placed = w.nas.path().join(folder).join("a.segy");
        assert_eq!(std::fs::read(placed).unwrap(), body, "{at}");
        if folder == "restore/x" {
            assert!(p.made.contains("restore/x"), "{at}: {p:?}");
            let m = std::fs::metadata(w.nas.path().join(folder)).unwrap().mode();
            assert_eq!(m & 0o777, 0o775, "{at}: its parent's");
        }
    }
}

#[tokio::test]
async fn an_outage_at_a_taken_name_isnt_a_reason_for_another_name() {
    // A second name for one inode, or a needless `-restored` copy.
    for (at, same) in [("p4_exist_stat", false), ("same_content", true)] {
        let w = world();
        let body = data(1000);
        let name = w.nas.path().join("lts/surveys/a.segy");
        std::fs::write(&name, if same { body.clone() } else { b"mine".to_vec() }).unwrap();
        let mut t = ticket(1, "lts/surveys", "a.segy", &body, true);
        t["same_content"] = json!(same);
        give(&w, t, &body);
        eio_at(at);
        pass(&w).await;
        assert_eq!(state(&w, 1).state, "linking", "{at}");
        let restored = w.nas.path().join("lts/surveys/a-restored.segy");
        assert!(!restored.exists(), "{at}: no other name yet");
        later(&w);
        placer_pass(&w, false).await;
        let p = state(&w, 1);
        assert_eq!(p.state, "done", "{at}: {p:?}");
        let want = if same {
            "already_there"
        } else {
            "restored_name"
        };
        assert_eq!(p.outcome.as_deref(), Some(want), "{at}");
        assert_eq!(restored.exists(), !same, "{at}");
    }
}

#[tokio::test]
async fn a_placeholder_cleanup_the_nas_cant_do_now_is_a_leftover() {
    let w = world();
    let body = data(1000);
    give(&w, ticket(1, "lts/surveys", "a.segy", &body, true), &body);
    faults::LINK_EACCES.with(|f| f.set(true));
    faults::STOP_AT.with(|s| *s.borrow_mut() = Some("placeholder".into()));
    pass(&w).await;
    faults::LINK_EACCES.with(|f| f.set(false));
    assert_eq!(state(&w, 1).state, "placeholder");
    let name = w.nas.path().join("lts/surveys/a.segy");
    assert!(name.exists());
    w.svc.state.lock().unwrap().refuse_go = Some("acl_mismatch".into());
    eio_at("cleanup_placeholder");
    later(&w);
    placer_pass(&w, false).await;
    let p = state(&w, 1);
    assert_eq!(p.state, "failed", "{p:?}");
    assert!(p.leftover.is_some(), "said, not swallowed: {p:?}");
    assert!(name.exists());
    later(&w);
    placer_pass(&w, false).await;
    assert!(state(&w, 1).leftover.is_none());
    assert!(!name.exists(), "cleaned when it could be");
}

#[tokio::test]
async fn a_pointer_that_cant_be_moved_now_is_tried_again() {
    let w = world();
    let body = data(1000);
    let ptr = w.nas.path().join("lts/surveys/a.segy.omnifile.txt");
    let text =
        crate::annex::remover::pointer_text("lts", "surveys/a.segy", 9, "2026-10-01T00:00:00Z");
    std::fs::write(&ptr, &text).unwrap();
    give(&w, ticket(1, "lts/surveys", "a.segy", &body, true), &body);
    eio_at("p6_rename");
    pass(&w).await;
    assert_eq!(state(&w, 1).state, "owned");
    assert!(ptr.exists());
    later(&w);
    placer_pass(&w, false).await;
    assert_eq!(state(&w, 1).pointer_deleted, Some(true));
    assert!(!ptr.exists());
}

#[tokio::test]
async fn a_journal_that_cant_be_read_makes_no_orphans_and_no_new_folder() {
    let w = world();
    let body = data(1000);
    give(&w, ticket(1, "lts/surveys", "a.segy", &body, true), &body);
    faults::STOP_AT.with(|s| *s.borrow_mut() = Some("created".into()));
    pass(&w).await;
    let said = |w: &World| {
        w.db.outbox(100)
            .unwrap()
            .into_iter()
            .filter(|e| e.3["reason"] == "staging_orphan")
            .count()
    };
    eio_at("staging_names");
    faults::STOP_AT.with(|s| *s.borrow_mut() = Some("filled".into()));
    later(&w);
    placer_pass(&w, false).await;
    assert_eq!(said(&w), 0, "the staged file isn't an orphan");
    // No meta to read: an error, not "no folder yet, make one".
    let folders = staging_folders(&w);
    w.db.conn()
        .execute("ALTER TABLE meta RENAME TO meta_away", [])
        .unwrap();
    give(&w, ticket(2, "lts/surveys", "b.segy", &body, true), &body);
    pass(&w).await;
    w.db.conn()
        .execute("ALTER TABLE meta_away RENAME TO meta", [])
        .unwrap();
    assert_eq!(staging_folders(&w), folders);
    assert_eq!(state(&w, 2).state, "ticketed");
    later(&w);
    placer_pass(&w, false).await;
    assert_eq!(state(&w, 2).state, "done");
}

#[tokio::test]
async fn a_journal_read_that_fails_isnt_no_folder_yet() {
    // It looked like a folder made and never locked, holding someone's files.
    let w = world();
    let body = data(1000);
    give(&w, ticket(1, "lts/surveys", "a.segy", &body, true), &body);
    faults::STOP_AT.with(|s| *s.borrow_mut() = Some("filled".into()));
    pass(&w).await;
    eio_at("meta_read");
    later(&w);
    placer_pass(&w, false).await;
    let p = state(&w, 1);
    assert_eq!((p.state.as_str(), p.attempts), ("filled", 1), "{p:?}");
    later(&w);
    placer_pass(&w, false).await;
    assert_eq!(state(&w, 1).state, "done");
}

// --- the fourth review -----------------------------------------------------------

#[tokio::test]
async fn a_cancelled_placement_with_no_mount_doesnt_ask_for_one_every_tick() {
    // E-1: kinit and mount every 2 s, for good. Nothing made: no leftover at all.
    let w = world();
    let body = data(1000);
    give(&w, ticket(1, "lts/surveys", "a.segy", &body, true), &body);
    take_tickets(&w.svc, &w.db, ANNEX, w.now.get()).await;
    for _ in 0..MAX_ATTEMPTS {
        later(&w);
        no_mount_pass(&w.svc, &w.db, w.now.get(), "no keytab").await;
    }
    stuck_at(&w, 1, "ticketed");
    cancel(&w, 1);
    later(&w);
    no_mount_pass(&w.svc, &w.db, w.now.get(), "no keytab").await;
    let p = state(&w, 1);
    assert_eq!(p.state, "failed");
    assert!(p.leftover.is_none(), "nothing was made: {p:?}");
    later(&w);
    assert!(!Journal(&w.db).needs_mount(w.now.get()));
    // Something was: it asks, hourly and no more.
    let w = world();
    give(&w, ticket(1, "lts/surveys", "a.segy", &body, true), &body);
    faults::STOP_AT.with(|s| *s.borrow_mut() = Some("filled".into()));
    pass(&w).await;
    for _ in 0..MAX_ATTEMPTS {
        later(&w);
        no_mount_pass(&w.svc, &w.db, w.now.get(), "no keytab").await;
    }
    cancel(&w, 1);
    later(&w);
    no_mount_pass(&w.svc, &w.db, w.now.get(), "no keytab").await;
    assert!(state(&w, 1).leftover.is_some());
    later(&w);
    assert!(Journal(&w.db).needs_mount(w.now.get()), "due");
    no_mount_pass(&w.svc, &w.db, w.now.get(), "no keytab").await;
    assert!(
        !Journal(&w.db).needs_mount(w.now.get()),
        "not again before the hour"
    );
}

#[tokio::test]
async fn erofs_a_quota_or_a_full_disk_never_costs_a_whole_copy() {
    // E-2: they can clear. P3's ACL deleted the copy as acl_unsupported, and P5 left
    // the placement account as owner for good.
    for errno in [libc::EROFS, libc::EDQUOT, libc::ENOSPC] {
        assert!(
            !definite(&std::io::Error::from_raw_os_error(errno)),
            "{errno}"
        );
        let w = world();
        let body = data(1000);
        give(&w, ticket(1, "lts/surveys", "a.segy", &body, true), &body);
        faults::NEXT_ERRNO.with(|f| f.set(errno));
        eio_at("set_acl");
        pass(&w).await;
        faults::NEXT_ERRNO.with(|f| f.set(0));
        let p = state(&w, 1);
        assert_eq!(p.state, "filled", "{errno}: {p:?}");
        later(&w);
        placer_pass(&w, false).await;
        assert_eq!(state(&w, 1).state, "done", "{errno}");
    }
    for errno in [libc::EACCES, libc::EPERM, libc::EOPNOTSUPP, libc::EINVAL] {
        assert!(
            definite(&std::io::Error::from_raw_os_error(errno)),
            "{errno}"
        );
    }
    // And after P3's chown, the times, or the link.
    for at in ["p3_group", "p3_times", "p4_link"] {
        let w = world();
        let body = data(1000);
        give(&w, ticket(1, "lts/surveys", "a.segy", &body, true), &body);
        faults::NEXT_ERRNO.with(|f| f.set(libc::ENOSPC));
        eio_at(at);
        pass(&w).await;
        faults::NEXT_ERRNO.with(|f| f.set(0));
        let p = state(&w, 1);
        assert_ne!(p.state, "failed", "{at}: {p:?}");
        later(&w);
        placer_pass(&w, false).await;
        assert_eq!(state(&w, 1).state, "done", "{at}");
    }
    // P5 on a read-only remount: not done with the wrong owner.
    let w = world();
    let body = data(1000);
    give(&w, ticket(1, "lts/surveys", "a.segy", &body, true), &body);
    faults::NEXT_ERRNO.with(|f| f.set(libc::EROFS));
    eio_at("p5_owner");
    pass(&w).await;
    faults::NEXT_ERRNO.with(|f| f.set(0));
    assert_eq!(state(&w, 1).state, "linked");
    later(&w);
    placer_pass(&w, false).await;
    assert_eq!(state(&w, 1).owner_restored, Some(true));
}

#[tokio::test]
async fn go_says_whether_the_server_shows_posix_acls() {
    // SS1: `fgetxattr` reads a server that hides them and a file with none alike,
    // and a hidden mask makes 0640 mean something else.
    let w = world();
    let body = data(1000);
    give(&w, ticket(1, "lts/surveys", "a.segy", &body, true), &body);
    faults::ACLS_HIDDEN.with(|f| f.set(true));
    pass(&w).await;
    faults::ACLS_HIDDEN.with(|f| f.set(false));
    let go = w.svc.state.lock().unwrap().placement_steps.clone();
    let go = go.iter().find(|(_, b)| b["step"] == "go").unwrap();
    assert_eq!(go.1["posix_acls_shown"], false);
    // A file with no ACL, on a filesystem that has them, is said to show them.
    let d = tempfile::tempdir().unwrap();
    let f = std::fs::File::open(d.path()).unwrap();
    let real = crate::annex::acl::posix_acls_shown(&f);
    let w = world();
    give(&w, ticket(1, "lts/surveys", "a.segy", &body, true), &body);
    pass(&w).await;
    let go = w.svc.state.lock().unwrap().placement_steps.clone();
    let go = go.iter().find(|(_, b)| b["step"] == "go").unwrap();
    assert_eq!(go.1["posix_acls_shown"], real);
}

#[test]
fn over_smb_no_ids_are_looked_up() {
    // The one guard behind P3, `go` and P5: winbind being away is no matter there.
    let w = world();
    w.ids.1.store(true, Ordering::SeqCst);
    let pl = open_root(w.nas.path(), "").unwrap();
    let ro = open_root(w.nas.path(), "").unwrap();
    let mut p = Placer {
        svc: &w.svc,
        db: &w.db,
        pl: &pl,
        ro: &ro,
        annex_id: ANNEX,
        annex_name: "lts",
        annex_rel: "lts",
        flavour: Flavour::Smb,
        ids: &w.ids,
        placement_sid: None,
        read_sid: None,
        read_who: None,
        now: w.now.get(),
        state_dir: w.state.path(),
        accounts_problem: None,
        journal_failed: std::cell::Cell::new(false),
        grace_ms: GRACE_MS,
        smb: false,
    };
    let body = data(10);
    let t = Ticket::from_json(&ticket(1, "lts", "a.segy", &body, true)).unwrap();
    assert_eq!(p.final_ids(&t), Ok(None));
    p.flavour = Flavour::Nfs3;
    assert!(p.final_ids(&t).is_err(), "it would have asked");
}

#[tokio::test]
async fn gos_folder_acl_is_strict_too() {
    // The staged file's lookups can come from a cache while the folder's fail.
    let w = world();
    let body = data(1000);
    let mut t = ticket(1, "lts/surveys", "a.segy", &body, true);
    t["uid"] = json!(nix_uid());
    t["gid"] = json!(nix_gid());
    give(&w, t, &body);
    faults::STOP_AT.with(|s| *s.borrow_mut() = Some("secured".into()));
    pass(&w).await;
    // The staged file's owner and group, and then nothing.
    w.ids.2.store(2, Ordering::SeqCst);
    later(&w);
    placer_pass(&w, false).await;
    w.ids.2.store(-1, Ordering::SeqCst);
    let p = state(&w, 1);
    assert_eq!(p.state, "secured", "{p:?}");
    assert_eq!(p.attempts, 1);
    assert!(!steps(&w).iter().any(|s| s == "go"));
}

#[tokio::test]
async fn an_outage_reading_a_crashed_p0s_folder_isnt_not_ours() {
    let w = world();
    let body = data(1000);
    give(&w, ticket(1, "restore/x", "a.segy", &body, false), &body);
    faults::STOP_AT.with(|s| *s.borrow_mut() = Some("p0_made".into()));
    pass(&w).await;
    eio_at("our_folder");
    later(&w);
    placer_pass(&w, false).await;
    let p = state(&w, 1);
    assert_eq!((p.state.as_str(), p.attempts), ("ticketed", 1), "{p:?}");
    later(&w);
    placer_pass(&w, false).await;
    let p = state(&w, 1);
    assert_eq!(p.state, "done", "{p:?}");
    assert!(p.made.contains("restore/x"));
}

#[tokio::test]
async fn stuck_with_the_link_taken_and_ended_says_so_and_drops_the_second_link() {
    // nlink stayed 2, with no leftover, no open row and no report: the remover
    // would refuse the file for good.
    let w = world();
    let body = data(1000);
    give(&w, ticket(1, "lts/surveys", "a.segy", &body, true), &body);
    faults::STOP_AT.with(|s| *s.borrow_mut() = Some("link_taken".into()));
    pass(&w).await;
    faults::TARGET_STAT_EIO.with(|f| f.set(100));
    tries_run_out(&w).await;
    faults::TARGET_STAT_EIO.with(|f| f.set(0));
    stuck_at(&w, 1, "linking");
    let f = w.nas.path().join("lts/surveys/a.segy");
    assert_eq!(std::fs::metadata(&f).unwrap().nlink(), 2);
    // Where it is goes with the report.
    let stuck = w.svc.state.lock().unwrap().placement_steps.clone();
    let stuck = stuck.iter().rfind(|(_, b)| b["step"] == "stuck").unwrap();
    assert_eq!(stuck.1["at"], "linking");
    assert_eq!(stuck.1["name"], "a.segy");
    // Abandoned.
    w.svc.state.lock().unwrap().ended.insert(1, "failed".into());
    later(&w);
    placer_pass(&w, false).await;
    assert_eq!(state(&w, 1).state, "failed");
    assert_eq!(std::fs::metadata(&f).unwrap().nlink(), 1);
    assert_eq!(std::fs::read(&f).unwrap(), body, "the file stays");
    let steps = w.svc.state.lock().unwrap().placement_steps.clone();
    let left = steps
        .iter()
        .rfind(|(_, b)| b["step"] == "leftover")
        .unwrap();
    assert!(left.1["detail"].as_str().unwrap().contains("a.segy"));
    assert!(staging_empty(&w));
}

#[tokio::test]
async fn a_retry_the_journal_cant_take_says_so() {
    let w = world();
    let body = data(1000);
    give(&w, ticket(1, "lts/surveys", "a.segy", &body, true), &body);
    w.svc.state.lock().unwrap().read_status = Some(503);
    faults::RETRY_NOT_RECORDED.with(|f| f.set(true));
    let out = pass(&w).await;
    faults::RETRY_NOT_RECORDED.with(|f| f.set(false));
    assert!(out.journal_failed);
}

#[tokio::test]
async fn stuck_with_no_mount_still_says_where_it_is() {
    // The abandon's text comes from this, and an hourly report mustn't wipe it.
    let w = world();
    let body = data(1000);
    give(&w, ticket(1, "lts/surveys", "a.segy", &body, true), &body);
    faults::STOP_AT.with(|s| *s.borrow_mut() = Some("filled".into()));
    pass(&w).await;
    for _ in 0..MAX_ATTEMPTS {
        later(&w);
        no_mount_pass(&w.svc, &w.db, w.now.get(), "no keytab").await;
    }
    later(&w);
    no_mount_pass(&w.svc, &w.db, w.now.get(), "no keytab").await;
    let steps = w.svc.state.lock().unwrap().placement_steps.clone();
    let stuck: Vec<_> = steps.iter().filter(|(_, b)| b["step"] == "stuck").collect();
    assert!(!stuck.is_empty());
    assert!(stuck.iter().all(|(_, b)| b["at"] == "filled"), "{stuck:?}");
}

#[cfg(target_os = "linux")]
#[test]
fn a_filesystem_with_no_such_attribute_doesnt_show_acls() {
    // /proc has no ACLs at all, as a server that hides them answers.
    let f = std::fs::File::open("/proc/self/status").unwrap();
    assert!(!crate::annex::acl::posix_acls_shown(&f));
    // A file with none, where a filesystem has them.
    let d = tempfile::tempdir().unwrap();
    let p = d.path().join("a");
    std::fs::write(&p, b"x").unwrap();
    let f = std::fs::File::open(&p).unwrap();
    assert!(crate::annex::acl::posix_acls_shown(&f));
}

#[tokio::test]
async fn the_nas_back_ends_the_hour_a_placement_stuck_without_a_mount_would_wait() {
    let w = world();
    let body = data(1000);
    give(&w, ticket(1, "lts/surveys", "a.segy", &body, true), &body);
    take_tickets(&w.svc, &w.db, ANNEX, w.now.get()).await;
    for _ in 0..MAX_ATTEMPTS {
        later(&w);
        no_mount_pass(&w.svc, &w.db, w.now.get(), "mount timed out").await;
    }
    stuck_at(&w, 1, "ticketed");
    let now = w.now.get();
    assert!(state(&w, 1).next_at > now + 60_000);
    // Stuck for something a mount says nothing about: it keeps its hour.
    give(&w, ticket(2, "lts/surveys", "b.segy", &body, true), &body);
    take_tickets(&w.svc, &w.db, ANNEX, now).await;
    Journal(&w.db)
        .set(
            2,
            "stuck",
            &[
                ("reason", text("trouble: P3 ACL: Host is down".to_owned())),
                ("next_at", int(now + 3_600_000)),
            ],
            now,
        )
        .unwrap();
    mount_is_back(&w.db, now);
    assert_eq!(state(&w, 1).next_at, now);
    assert_eq!(state(&w, 2).next_at, now + 3_600_000);
    // And it goes on from there.
    placer_pass(&w, false).await;
    assert_eq!(state(&w, 1).state, "done");
}

#[tokio::test]
async fn a_refusal_of_a_staged_file_still_ends_it() {
    // What can clear waits; what says no doesn't.
    for at in ["p3_group", "p3_times"] {
        let w = world();
        let body = data(1000);
        give(&w, ticket(1, "lts/surveys", "a.segy", &body, true), &body);
        faults::NEXT_ERRNO.with(|f| f.set(libc::EACCES));
        eio_at(at);
        pass(&w).await;
        faults::NEXT_ERRNO.with(|f| f.set(0));
        let p = state(&w, 1);
        assert_eq!(
            (p.state.as_str(), p.reason.as_deref()),
            ("failed", Some("access_denied")),
            "{at}"
        );
        assert!(staging_empty(&w), "{at}");
    }
}

#[tokio::test]
async fn a_cancel_while_it_tries_with_no_mount_is_heard_when_it_sticks() {
    // The exit test's hang stage found this: the report that it stuck was told, and
    // nothing heard it for another hour.
    let w = world();
    let body = data(1000);
    give(&w, ticket(1, "lts/surveys", "a.segy", &body, true), &body);
    take_tickets(&w.svc, &w.db, ANNEX, w.now.get()).await;
    cancel(&w, 1);
    for _ in 0..MAX_ATTEMPTS {
        later(&w);
        no_mount_pass(&w.svc, &w.db, w.now.get(), "mount timed out").await;
    }
    assert_eq!(state(&w, 1).state, "stuck");
    report_all(&w.svc, &w.db, w.now.get()).await;
    no_mount_pass(&w.svc, &w.db, w.now.get(), "mount timed out").await;
    let p = state(&w, 1);
    assert_eq!(p.state, "failed", "{p:?}");
    assert!(p.leftover.is_none(), "nothing was made: {p:?}");
}

#[tokio::test]
async fn a_refusal_of_the_owner_that_clears_leaves_the_right_owner() {
    // A NAS's id map can say EPERM for a moment.
    let w = world();
    let body = data(1000);
    give(&w, ticket(1, "lts/surveys", "a.segy", &body, true), &body);
    faults::P5_SET_REFUSED.with(|f| f.set(true));
    pass(&w).await;
    faults::P5_SET_REFUSED.with(|f| f.set(false));
    later(&w);
    placer_pass(&w, false).await;
    let p = state(&w, 1);
    assert_eq!((p.state.as_str(), p.owner_restored), ("done", Some(true)));
}

#[tokio::test]
async fn a_server_that_cant_do_owners_needs_no_more_tries() {
    for errno in [libc::EOPNOTSUPP, libc::EINVAL] {
        let w = world();
        let body = data(1000);
        give(&w, ticket(1, "lts/surveys", "a.segy", &body, true), &body);
        faults::NEXT_ERRNO.with(|f| f.set(errno));
        eio_at("p5_owner");
        pass(&w).await;
        faults::NEXT_ERRNO.with(|f| f.set(0));
        let p = state(&w, 1);
        assert_eq!(
            (p.state.as_str(), p.owner_restored),
            ("done", Some(false)),
            "{errno}"
        );
    }
}

#[tokio::test]
async fn a_passing_refusal_of_a_crashed_p0s_folder_doesnt_disown_it() {
    let w = world();
    let body = data(1000);
    give(&w, ticket(1, "restore/x", "a.segy", &body, false), &body);
    faults::STOP_AT.with(|s| *s.borrow_mut() = Some("p0_made".into()));
    pass(&w).await;
    faults::NEXT_ERRNO.with(|f| f.set(libc::EACCES));
    eio_at("our_folder");
    later(&w);
    placer_pass(&w, false).await;
    faults::NEXT_ERRNO.with(|f| f.set(0));
    let p = state(&w, 1);
    assert_eq!((p.state.as_str(), p.attempts), ("ticketed", 1), "{p:?}");
    later(&w);
    placer_pass(&w, false).await;
    let p = state(&w, 1);
    assert_eq!(p.state, "done", "{p:?}");
    assert!(p.made.contains("restore/x"), "still ours: {p:?}");
}

#[tokio::test]
async fn a_folder_the_placement_account_is_always_refused_isnt_ours() {
    // Not stuck for good over a folder that can't be its own: not ours, after the tries.
    let w = world();
    let body = data(1000);
    give(&w, ticket(1, "restore/x", "a.segy", &body, false), &body);
    faults::STOP_AT.with(|s| *s.borrow_mut() = Some("p0_made".into()));
    pass(&w).await;
    faults::NEXT_ERRNO.with(|f| f.set(libc::EACCES));
    for _ in 1..MAX_ATTEMPTS {
        eio_at("our_folder");
        later(&w);
        placer_pass(&w, false).await;
        assert_eq!(state(&w, 1).state, "ticketed");
    }
    eio_at("our_folder");
    later(&w);
    placer_pass(&w, false).await;
    faults::NEXT_ERRNO.with(|f| f.set(0));
    let p = state(&w, 1);
    assert_ne!(p.state, "stuck", "{p:?}");
    assert!(
        !p.made.contains("restore/x"),
        "not taken as made by us: {p:?}"
    );
}

// --- A3.4: a replace at the origin ----------------------------------------------------

thread_local! {
    /// The placer runs as over SMB (the CRC gate), its streams faked away.
    static SMB_ON: std::cell::Cell<bool> = const { std::cell::Cell::new(false) };
}

const OLD: &[u8] = b"the version on the NAS now, verified in Azure";

fn old_path(w: &World) -> std::path::PathBuf {
    w.nas.path().join("lts/surveys/a.segy")
}

/// A ticket for an older version, whose replace expects the file at the name as
/// it is now (its attributes) with `crc` (the verified copy's).
fn rticket(w: &World, body: &[u8], crc_of: &[u8]) -> Value {
    let md = std::fs::metadata(old_path(w)).unwrap();
    let mut t = ticket(1, "lts/surveys", "a.segy", body, true);
    t["replace"] = json!({
        "version_id": "2", "nas_file_id": md.ino().to_string(), "size": md.len(),
        "mtime_ns": (md.mtime() * 1_000_000_000 + md.mtime_nsec()).to_string(),
        "ctime_ns": (md.ctime() * 1_000_000_000 + md.ctime_nsec()).to_string(),
        "crc64": format!("{:016x}", crc(crc_of)),
        "hidden_name": replace::hidden_name(1, "a.segy"),
    });
    t
}

fn hidden_path(w: &World) -> std::path::PathBuf {
    w.nas
        .path()
        .join("lts/surveys")
        .join(replace::hidden_name(1, "a.segy"))
}

/// A world with the verified copy at the name, and a ticket to replace it.
fn replace_world(body: &[u8]) -> World {
    let w = world();
    std::fs::write(old_path(&w), OLD).unwrap();
    let t = rticket(&w, body, OLD);
    give(&w, t, body);
    w
}

fn past_grace(w: &World) {
    w.now.set(w.now.get() + GRACE_MS + 1);
}

fn asked(w: &World, step: &str) -> Vec<Value> {
    w.svc
        .state
        .lock()
        .unwrap()
        .placement_steps
        .iter()
        .filter(|(_, b)| b["step"] == step)
        .map(|(_, b)| b.clone())
        .collect()
}

fn answer(w: &World, key: &str, v: Value) {
    w.svc
        .state
        .lock()
        .unwrap()
        .replace_answers
        .insert(key.into(), v);
}

fn listing(w: &World) -> Vec<String> {
    let mut v: Vec<String> = std::fs::read_dir(w.nas.path().join("lts/surveys"))
        .unwrap()
        .map(|e| e.unwrap().file_name().to_string_lossy().into_owned())
        .collect();
    v.sort();
    v
}

fn read(w: &World, name: &str) -> Vec<u8> {
    std::fs::read(w.nas.path().join("lts/surveys").join(name)).unwrap()
}

fn row(w: &World) -> replace::Row {
    w.db.conn()
        .query_row(
            "SELECT state, aside_name, reason FROM placement_replace WHERE placement_id = 1",
            [],
            |r| {
                Ok(replace::Row {
                    state: r.get(0)?,
                    aside_name: r.get(1)?,
                    reason: r.get(2)?,
                    ..Default::default()
                })
            },
        )
        .unwrap()
}

/// The one `-replaced-` name in the folder, if any.
fn aside(w: &World) -> Option<String> {
    listing(w).into_iter().find(|n| n.contains("-replaced-"))
}

#[tokio::test]
async fn a_verified_copy_at_the_name_is_replaced_after_the_grace() {
    let body = data(3 << 20);
    let w = replace_world(&body);
    pass(&w).await;
    // Hidden, and the name empty for the grace.
    assert!(!old_path(&w).exists(), "{:?}", state(&w, 1));
    assert_eq!(std::fs::read(hidden_path(&w)).unwrap(), OLD);
    assert_eq!(state(&w, 1).state, "replace_hidden");
    assert_eq!(row(&w).state, "hidden");
    assert_eq!(asked(&w, "replace_go").len(), 1);
    assert_eq!(
        asked(&w, "replace_hidden")[0]["hidden_name"],
        ".omnifile-replacing-1-a.segy"
    );
    assert!(
        replacing(&w.db, "surveys/a.segy"),
        "its departure isn't reported"
    );
    // In the grace, nothing moves, even asked to (its own clock, not only its next_at).
    w.db.conn()
        .execute("UPDATE placement SET next_at = 0", [])
        .unwrap();
    pass(&w).await;
    assert!(!old_path(&w).exists() && hidden_path(&w).exists());
    assert!(asked(&w, "replace_delete_go").is_empty());
    past_grace(&w);
    pass(&w).await;
    let p = state(&w, 1);
    assert_eq!(
        (p.state.as_str(), p.outcome.as_deref()),
        ("done", Some("replaced"))
    );
    assert_eq!(read(&w, "a.segy"), body);
    assert!(!hidden_path(&w).exists(), "the old file deleted");
    assert_eq!(listing(&w), ["a.segy"]);
    let phases: Vec<Value> = asked(&w, "replace_delete_go")
        .iter()
        .map(|b| b["phase"].clone())
        .collect();
    assert_eq!(phases, [json!("place"), json!("delete")]);
    assert_eq!(asked(&w, "replaced").len(), 1);
    assert_eq!(last_done(&w)["outcome"], "replaced");
    assert!(!replacing(&w.db, "surveys/a.segy"));
    assert!(staging_empty(&w));
}

#[tokio::test]
async fn a_file_changed_since_its_copy_is_never_hidden() {
    for how in ["append", "mtime", "chmod", "same_size", "second_link"] {
        let body = data(1000);
        let w = world();
        std::fs::write(old_path(&w), OLD).unwrap();
        let t = rticket(&w, &body, OLD);
        // Changed after the copy the ticket names, before anything is asked.
        let path = old_path(&w);
        let mtime = std::fs::metadata(&path).unwrap().modified().unwrap();
        match how {
            "append" => {
                use std::io::Write;
                std::fs::OpenOptions::new()
                    .append(true)
                    .open(&path)
                    .unwrap()
                    .write_all(b"!")
                    .unwrap();
            }
            "mtime" => {
                let f = std::fs::File::options().write(true).open(&path).unwrap();
                f.set_modified(mtime + std::time::Duration::from_secs(5))
                    .unwrap();
            }
            "chmod" => {
                std::fs::set_permissions(&path, std::fs::Permissions::from_mode(0o600)).unwrap()
            }
            "same_size" => {
                let mut b = OLD.to_vec();
                b[0] ^= 0xff;
                std::fs::write(&path, &b).unwrap();
                let f = std::fs::File::options().write(true).open(&path).unwrap();
                f.set_modified(mtime).unwrap(); // the mtime put back: the ctime tells
            }
            _ => std::fs::hard_link(&path, w.nas.path().join("lts/second")).unwrap(),
        }
        let before = std::fs::read(&path).unwrap();
        give(&w, t, &body);
        pass(&w).await;
        assert!(!hidden_path(&w).exists(), "{how}: never hidden");
        assert_eq!(std::fs::read(&path).unwrap(), before, "{how}: untouched");
        assert_eq!(read(&w, "a-restored.segy"), body, "{how}");
        assert!(asked(&w, "replace_go").is_empty(), "{how}: not even asked");
        let why = asked(&w, "replace_put_back")[0]["reason"].clone();
        let want = if how == "second_link" {
            "hard_linked"
        } else {
            "changed"
        };
        assert_eq!(why, want, "{how}");
        assert_eq!(
            state(&w, 1).outcome.as_deref(),
            Some("restored_name"),
            "{how}"
        );
    }
}

#[tokio::test]
async fn over_smb_the_crc_is_read_again_before_the_hide() {
    let body = data(1000);
    let w = world();
    // Other bytes of the same size, as an SMB client's write with its
    // ChangeTime set back would look: the ticket's attributes are what's there.
    let mut other = OLD.to_vec();
    other[3] ^= 0x55;
    std::fs::write(old_path(&w), &other).unwrap();
    let t = rticket(&w, &body, OLD);
    give(&w, t, &body);
    crate::annex::remover::faults::SMB
        .lock()
        .unwrap()
        .push((w.nas.path().to_path_buf(), None));
    SMB_ON.with(|x| x.set(true));
    pass(&w).await;
    SMB_ON.with(|x| x.set(false));
    assert_eq!(read(&w, "a.segy"), other, "untouched");
    assert!(!hidden_path(&w).exists());
    assert_eq!(asked(&w, "replace_put_back")[0]["reason"], "crc_mismatch");
}

#[tokio::test]
async fn an_smb_sharing_violation_at_the_hide_is_in_use() {
    let body = data(1000);
    let w = replace_world(&body);
    replace::faults::W1_ERRNO.with(|x| x.set(libc::EBUSY));
    pass(&w).await;
    assert_eq!(read(&w, "a.segy"), OLD);
    assert_eq!(read(&w, "a-restored.segy"), body);
    assert_eq!(asked(&w, "replace_put_back")[0]["reason"], "in_use");
    assert_eq!(row(&w).state, "refused");
}

#[tokio::test]
async fn a_write_before_the_hide_survives_under_its_own_name() {
    let body = data(1000);
    let w = replace_world(&body);
    let p = old_path(&w);
    replace::faults::at("before_w1", move || {
        use std::io::Write;
        std::fs::OpenOptions::new()
            .append(true)
            .open(&p)
            .unwrap()
            .write_all(b" and a late line")
            .unwrap();
    });
    pass(&w).await;
    past_grace(&w);
    pass(&w).await;
    let mut want = OLD.to_vec();
    want.extend_from_slice(b" and a late line");
    assert_eq!(read(&w, "a.segy"), want, "back under its own name");
    assert_eq!(read(&w, "a-restored.segy"), body);
    assert!(!hidden_path(&w).exists());
    assert_eq!(row(&w).state, "put_back");
    assert_eq!(state(&w, 1).outcome.as_deref(), Some("restored_name"));
}

#[tokio::test]
async fn a_write_in_the_grace_survives_under_its_own_name() {
    let body = data(1000);
    let w = replace_world(&body);
    pass(&w).await;
    {
        use std::io::Write;
        std::fs::OpenOptions::new()
            .append(true)
            .open(hidden_path(&w))
            .unwrap()
            .write_all(b"+")
            .unwrap();
    }
    past_grace(&w);
    pass(&w).await;
    let mut want = OLD.to_vec();
    want.push(b'+');
    assert_eq!(read(&w, "a.segy"), want);
    assert_eq!(read(&w, "a-restored.segy"), body);
    assert!(asked(&w, "replace_delete_go").is_empty(), "never asked");
    assert_eq!(
        asked(&w, "replace_put_back")[0]["reason"],
        "changed_in_grace"
    );
}

#[tokio::test]
async fn a_write_between_the_link_and_the_delete_survives_under_a_visible_name() {
    for point in ["r4", "before_w2"] {
        let body = data(1000);
        let w = replace_world(&body);
        pass(&w).await;
        let h = hidden_path(&w);
        replace::faults::at(point, move || {
            use std::io::Write;
            std::fs::OpenOptions::new()
                .append(true)
                .open(&h)
                .unwrap()
                .write_all(b"*")
                .unwrap();
        });
        past_grace(&w);
        pass(&w).await;
        let p = state(&w, 1);
        assert_eq!(
            (p.state.as_str(), p.outcome.as_deref()),
            ("done", Some("replaced_old_kept")),
            "{point}"
        );
        assert_eq!(read(&w, "a.segy"), body, "{point}");
        let kept = aside(&w).expect("kept aside");
        assert!(
            kept.starts_with("a-replaced-") && kept.ends_with("Z.segy"),
            "{kept}"
        );
        let mut want = OLD.to_vec();
        want.push(b'*');
        assert_eq!(read(&w, &kept), want, "{point}: the late write is there");
        assert!(!hidden_path(&w).exists());
        let rep = &asked(&w, "replace_aside")[0];
        assert_eq!(rep["name"], json!(kept));
        assert_eq!(rep["reason"], "changed_in_grace");
        assert!(asked(&w, "replaced").is_empty(), "{point}: nothing deleted");
    }
}

/// #98's review, E1: a file a user made at the name in the grace means nothing
/// replaced the old one, so it is never deleted: it is kept under a visible
/// `-replaced-` name, also after a restart while the new file is linked.
#[tokio::test]
async fn a_name_made_in_the_grace_is_never_replaced() {
    for stop in [None, Some("linking"), Some("linked")] {
        let body = data(1000);
        let w = replace_world(&body);
        pass(&w).await;
        std::fs::write(old_path(&w), b"a user's new file").unwrap();
        past_grace(&w);
        if let Some(at) = stop {
            faults::STOP_AT.with(|s| *s.borrow_mut() = Some(at.into()));
        }
        for _ in 0..3 {
            pass(&w).await;
        }
        assert_eq!(read(&w, "a.segy"), b"a user's new file", "{stop:?}");
        assert_eq!(read(&w, "a-restored.segy"), body, "{stop:?}");
        assert!(!hidden_path(&w).exists(), "{stop:?}");
        let kept = aside(&w).unwrap_or_else(|| panic!("{stop:?}: kept aside"));
        assert_eq!(read(&w, &kept), OLD, "{stop:?}: every byte of the old file");
        assert_eq!(row(&w).state, "put_aside", "{stop:?}");
        assert!(
            asked(&w, "replaced").is_empty(),
            "{stop:?}: nothing deleted"
        );
        assert_eq!(
            asked(&w, "replace_aside")[0]["name"],
            json!(kept),
            "{stop:?}"
        );
        let p = state(&w, 1);
        assert_eq!(
            (p.state.as_str(), p.outcome.as_deref()),
            ("done", Some("restored_name")),
            "{stop:?}"
        );
    }
}

/// E1: the delete question names the file's final name, so the service can
/// refuse a delete where the new file isn't at the origin.
#[tokio::test]
async fn the_delete_question_names_the_new_files_name() {
    let body = data(1000);
    let w = replace_world(&body);
    pass(&w).await;
    past_grace(&w);
    pass(&w).await;
    let del: Vec<Value> = asked(&w, "replace_delete_go")
        .into_iter()
        .filter(|b| b["phase"] == "delete")
        .collect();
    assert_eq!(del.len(), 1);
    assert_eq!(del[0]["name"], "a.segy");
}

/// E1: the new file swapped for a user's after its link: nothing replaced the old
/// file, so it is kept aside, never deleted.
#[tokio::test]
async fn a_user_file_at_the_name_after_the_link_keeps_the_old_one() {
    let body = data(1000);
    let w = replace_world(&body);
    pass(&w).await;
    let at = old_path(&w);
    replace::faults::at("before_w2_name", move || {
        let tmp = at.with_extension("tmp");
        std::fs::write(&tmp, b"a user's save over the new file").unwrap();
        std::fs::rename(&tmp, &at).unwrap();
    });
    past_grace(&w);
    pass(&w).await;
    assert_eq!(read(&w, "a.segy"), b"a user's save over the new file");
    let kept = aside(&w).expect("kept aside");
    assert_eq!(read(&w, &kept), OLD);
    assert!(asked(&w, "replaced").is_empty(), "nothing deleted");
    assert!(!hidden_path(&w).exists());
}

/// #98's review, E2: a save-by-rename just before W1 means W1 hid the user's new
/// file. It is never taken as "nothing hidden": it goes back under its own name,
/// and the new file takes a `-restored` one. Also found so after a restart.
#[tokio::test]
async fn a_users_file_swapped_in_before_the_hide_goes_back() {
    for stop in [None, Some("replace_w1_taken")] {
        let body = data(1000);
        let w = replace_world(&body);
        let at = old_path(&w);
        replace::faults::at("before_w1", move || {
            let tmp = at.with_extension("tmp");
            std::fs::write(&tmp, b"the user's newer save").unwrap();
            std::fs::rename(&tmp, &at).unwrap();
        });
        if let Some(s) = stop {
            faults::STOP_AT.with(|x| *x.borrow_mut() = Some(s.into()));
        }
        for _ in 0..3 {
            pass(&w).await;
        }
        assert_eq!(read(&w, "a.segy"), b"the user's newer save", "{stop:?}");
        assert_eq!(read(&w, "a-restored.segy"), body, "{stop:?}");
        assert!(!hidden_path(&w).exists(), "{stop:?}: never left hidden");
        assert_eq!(
            asked(&w, "replace_put_back")[0]["reason"],
            "swapped_at_hide",
            "{stop:?}"
        );
        assert_eq!(state(&w, 1).outcome.as_deref(), Some("restored_name"));
        assert!(
            !replacing(&w.db, "surveys/a.segy"),
            "{stop:?}: the replace closed"
        );
    }
}

/// E2/E6: a placement that ends with a user's file under its hidden name (W1 hid
/// it, then a restart, then the end) puts that file back too.
#[tokio::test]
async fn the_placement_ending_puts_a_swapped_file_back() {
    let body = data(1000);
    let w = replace_world(&body);
    let at = old_path(&w);
    replace::faults::at("before_w1", move || {
        let tmp = at.with_extension("tmp");
        std::fs::write(&tmp, b"the user's newer save").unwrap();
        std::fs::rename(&tmp, &at).unwrap();
    });
    faults::STOP_AT.with(|x| *x.borrow_mut() = Some("replace_w1_taken".into()));
    pass(&w).await;
    assert!(!old_path(&w).exists(), "the user's file hidden");
    // The placement ends (P7) before the hide is looked at again.
    let p = state(&w, 1);
    assert!(with_placer(&w, |pl| pl.unwind_old(&p)).is_none());
    assert_eq!(read(&w, "a.segy"), b"the user's newer save");
    assert!(!hidden_path(&w).exists());
    assert!(!replacing(&w.db, "surveys/a.segy"));
}

/// #98's review, E3: a crash between W1 and the journal of its ctime, and a
/// same-size rewrite with its mtime set back while the edge was down (over NFS
/// the ctime alone would have been taken as the baseline). The bytes are read
/// again: the rewrite goes back under its own name, nothing is deleted.
#[tokio::test]
async fn a_write_while_the_edge_was_down_after_the_hide_is_never_deleted() {
    let body = data(1000);
    let w = replace_world(&body);
    faults::STOP_AT.with(|x| *x.borrow_mut() = Some("replace_w1_taken".into()));
    pass(&w).await;
    assert!(hidden_path(&w).exists());
    let h = hidden_path(&w);
    let mtime = std::fs::metadata(&h).unwrap().modified().unwrap();
    let mut other = OLD.to_vec();
    other[0] ^= 0xff;
    std::fs::OpenOptions::new()
        .write(true)
        .open(&h)
        .and_then(|mut f| {
            use std::io::Write;
            f.write_all(&other)
        })
        .unwrap();
    std::fs::File::options()
        .write(true)
        .open(&h)
        .unwrap()
        .set_modified(mtime)
        .unwrap();
    for _ in 0..3 {
        pass(&w).await;
        past_grace(&w);
    }
    assert_eq!(
        read(&w, "a.segy"),
        other,
        "the rewrite, back under its name"
    );
    assert_eq!(read(&w, "a-restored.segy"), body);
    assert!(asked(&w, "replaced").is_empty(), "nothing deleted");
    assert_eq!(
        asked(&w, "replace_put_back")[0]["reason"],
        "crc_mismatch_while_down"
    );
    // Unchanged while down: the replace goes on.
    let w = replace_world(&body);
    faults::STOP_AT.with(|x| *x.borrow_mut() = Some("replace_w1_taken".into()));
    pass(&w).await;
    for _ in 0..3 {
        pass(&w).await;
        past_grace(&w);
    }
    assert_eq!(state(&w, 1).outcome.as_deref(), Some("replaced"));
}

/// #98's review, E5: a name too long for its hidden name is no bad ticket: the
/// replace is given up and the file takes a `-restored` name.
#[tokio::test]
async fn a_name_too_long_to_hide_takes_a_restored_name() {
    let body = data(1000);
    let w = world();
    let name = format!("{}.segy", "x".repeat(240));
    let path = w.nas.path().join("lts/surveys").join(&name);
    std::fs::write(&path, OLD).unwrap();
    let md = std::fs::metadata(&path).unwrap();
    let mut t = ticket(1, "lts/surveys", &name, &body, true);
    t["names"] = json!([name, "a-restored.segy"]);
    t["replace"] = json!({
        "version_id": "2", "nas_file_id": md.ino().to_string(), "size": md.len(),
        "mtime_ns": (md.mtime() * 1_000_000_000 + md.mtime_nsec()).to_string(),
        "ctime_ns": (md.ctime() * 1_000_000_000 + md.ctime_nsec()).to_string(),
        "crc64": format!("{:016x}", crc(OLD)),
        "hidden_name": replace::hidden_name(1, &name),
    });
    assert!(Ticket::from_json(&t).is_ok(), "not a bad ticket");
    give(&w, t, &body);
    pass(&w).await;
    assert_eq!(std::fs::read(&path).unwrap(), OLD, "untouched");
    assert_eq!(read(&w, "a-restored.segy"), body);
    assert_eq!(asked(&w, "replace_put_back")[0]["reason"], "name_too_long");
    assert!(asked(&w, "replace_go").is_empty());
}

/// #98's review, E7: W3's placeholder left at a `-replaced-` name the replace
/// gave up on is removed (it is still exactly ours) before the journal forgets it.
#[tokio::test]
async fn a_placeholder_at_a_name_given_up_is_removed() {
    let body = data(1000);
    let w = replace_world(&body);
    pass(&w).await;
    let dir = open_root(w.nas.path(), "")
        .unwrap()
        .sub("lts/surveys")
        .unwrap();
    let left = "a-replaced-2026-01-01-000000Z.segy";
    let (pino, pctime) = crate::annex::remover::imp::make_placeholder(&dir, left).unwrap();
    w.db.conn()
        .execute(
            "UPDATE placement_replace SET placeholder_ino = ?1, placeholder_ctime = ?2, \
             placeholder_name = ?3 WHERE placement_id = 1",
            rusqlite::params![pino as i64, pctime, left],
        )
        .unwrap();
    answer(
        &w,
        "replace_delete_go:place",
        json!({"proceed": false, "state": "failed", "reason": "access_denied"}),
    ); // fmt: skip
    past_grace(&w);
    pass(&w).await;
    assert_eq!(read(&w, "a.segy"), OLD, "back under its own name");
    assert_eq!(listing(&w), ["a.segy"], "no placeholder left");
}

#[tokio::test]
async fn a_refusal_after_the_grace_puts_the_old_file_back() {
    let body = data(1000);
    let w = replace_world(&body);
    pass(&w).await;
    // A hold first: it waits, hidden.
    answer(
        &w,
        "replace_delete_go:place",
        json!({"proceed": false, "hold": true, "reason": "paused"}),
    ); // fmt: skip
    past_grace(&w);
    pass(&w).await;
    assert!(hidden_path(&w).exists() && !old_path(&w).exists());
    assert_eq!(state(&w, 1).state, "replace_hidden");
    // Then a definite refusal (a legal hold placed in the grace).
    answer(
        &w,
        "replace_delete_go:place",
        json!({"proceed": false, "hold": false, "reason": "legal_hold"}),
    ); // fmt: skip
    w.now.set(w.now.get() + PAUSE_RETRY_MS + 1);
    pass(&w).await;
    assert_eq!(read(&w, "a.segy"), OLD, "back under its own name");
    assert_eq!(read(&w, "a-restored.segy"), body);
    assert_eq!(asked(&w, "replace_put_back")[0]["reason"], "legal_hold");
}

#[tokio::test]
async fn a_refusal_at_the_delete_keeps_the_old_file_aside() {
    let body = data(1000);
    let w = replace_world(&body);
    pass(&w).await;
    answer(
        &w,
        "replace_delete_go:delete",
        json!({"proceed": false, "hold": false, "reason": "legal_hold"}),
    ); // fmt: skip
    past_grace(&w);
    pass(&w).await;
    assert_eq!(read(&w, "a.segy"), body);
    let kept = aside(&w).expect("kept aside");
    assert_eq!(read(&w, &kept), OLD);
    assert_eq!(state(&w, 1).outcome.as_deref(), Some("replaced_old_kept"));
    assert_eq!(asked(&w, "replace_aside")[0]["reason"], "legal_hold");
}

#[tokio::test]
async fn an_unreachable_service_never_deletes() {
    let body = data(1000);
    let w = replace_world(&body);
    pass(&w).await;
    w.svc.state.lock().unwrap().down = true;
    past_grace(&w);
    for _ in 0..(MAX_ATTEMPTS + 2) {
        pass(&w).await;
        w.now.set(w.now.get() + 20 * 60_000);
    }
    let p = state(&w, 1);
    assert_eq!(p.state, "stuck", "{p:?}");
    assert!(
        p.reason
            .as_deref()
            .unwrap_or("")
            .contains(".omnifile-replacing-1-a.segy"),
        "the stuck report names the hidden file: {:?}",
        p.reason
    );
    assert_eq!(
        std::fs::read(hidden_path(&w)).unwrap(),
        OLD,
        "every byte kept"
    );
    assert!(!old_path(&w).exists());
    // The service back: it goes on from where it was.
    w.svc.state.lock().unwrap().down = false;
    later(&w);
    pass(&w).await;
    assert_eq!(state(&w, 1).outcome.as_deref(), Some("replaced"));
    assert_eq!(read(&w, "a.segy"), body);
}

#[tokio::test]
async fn the_placement_ending_puts_the_old_file_back() {
    let body = data(1000);
    let w = replace_world(&body);
    pass(&w).await;
    answer(
        &w,
        "replace_delete_go:place",
        json!({"proceed": false, "state": "failed", "reason": "access_denied"}),
    ); // fmt: skip
    past_grace(&w);
    pass(&w).await;
    let p = state(&w, 1);
    assert_eq!(
        (p.state.as_str(), p.reason.as_deref()),
        ("failed", Some("access_denied"))
    );
    assert_eq!(read(&w, "a.segy"), OLD, "back under its own name");
    assert!(!hidden_path(&w).exists());
    assert!(staging_empty(&w), "the staged copy goes");
    assert_eq!(listing(&w), ["a.segy"]);
    assert_eq!(asked(&w, "replace_put_back").len(), 1);
}

#[tokio::test]
async fn a_refused_put_back_is_stuck_and_keeps_every_byte() {
    let body = data(1000);
    let w = replace_world(&body);
    pass(&w).await;
    answer(
        &w,
        "replace_delete_go:place",
        json!({"proceed": false, "hold": false, "reason": "legal_hold"}),
    ); // fmt: skip
    replace::faults::W3_REFUSED.with(|x| x.set(true));
    past_grace(&w);
    pass(&w).await;
    replace::faults::W3_REFUSED.with(|x| x.set(false));
    let p = state(&w, 1);
    assert_eq!(p.state, "stuck");
    assert!(p.reason.unwrap().contains(".omnifile-replacing-1-a.segy"));
    assert_eq!(std::fs::read(hidden_path(&w)).unwrap(), OLD);
    assert!(!staging_empty(&w), "the staged copy is kept too");
    // An hour on, it is tried again, and goes back.
    later(&w);
    pass(&w).await;
    assert_eq!(read(&w, "a.segy"), OLD);
    assert_eq!(read(&w, "a-restored.segy"), body);
}

#[tokio::test]
async fn a_failed_journal_write_stops_before_its_write() {
    // Before the hide.
    let body = data(1000);
    let w = replace_world(&body);
    faults::STOP_AT.with(|s| *s.borrow_mut() = Some("replace_start".into()));
    pass(&w).await;
    faults::JOURNAL_FAILS.with(|f| f.set(true));
    pass(&w).await;
    faults::JOURNAL_FAILS.with(|f| f.set(false));
    assert_eq!(read(&w, "a.segy"), OLD, "never hidden");
    assert!(!hidden_path(&w).exists());
    // Before the delete.
    let w = replace_world(&body);
    pass(&w).await;
    replace::faults::at("before_w2", || faults::JOURNAL_FAILS.with(|f| f.set(true)));
    past_grace(&w);
    pass(&w).await;
    faults::JOURNAL_FAILS.with(|f| f.set(false));
    assert_eq!(
        std::fs::read(hidden_path(&w)).unwrap(),
        OLD,
        "never deleted"
    );
    pass(&w).await;
    assert_eq!(state(&w, 1).outcome.as_deref(), Some("replaced"));
    assert!(!hidden_path(&w).exists());
}

#[tokio::test]
async fn a_kill_at_every_step_resumes_and_never_deletes_twice() {
    for at in [
        "replace_start",
        "replace_hiding",
        "replace_w1_taken",
        "replace_hidden",
        "replace_grace_passed",
        "linking",
        "linked",
        "replace_deleting",
        "replace_old_deleted",
    ] {
        let body = data(1000);
        let w = replace_world(&body);
        faults::STOP_AT.with(|s| *s.borrow_mut() = Some(at.into()));
        for _ in 0..4 {
            pass(&w).await;
            past_grace(&w);
        }
        let p = state(&w, 1);
        assert_eq!(
            (p.state.as_str(), p.outcome.as_deref()),
            ("done", Some("replaced")),
            "{at}: {p:?}"
        );
        assert_eq!(read(&w, "a.segy"), body, "{at}");
        assert_eq!(listing(&w), ["a.segy"], "{at}");
        assert_eq!(asked(&w, "replaced").len(), 1, "{at}: deleted once");
        assert_eq!(asked(&w, "replace_hidden").len(), 1, "{at}: hidden once");
    }
    // And on the way back: a kill while putting the old file back or aside.
    for (at, phase) in [
        ("replace_putting_back", "place"),
        ("replace_w3", "place"),
        ("replace_putting_aside", "delete"),
        ("replace_w3", "delete"),
    ] {
        let body = data(1000);
        let w = replace_world(&body);
        pass(&w).await;
        answer(
            &w,
            &format!("replace_delete_go:{phase}"),
            json!({"proceed": false, "reason": "legal_hold"}),
        ); // fmt: skip
        faults::STOP_AT.with(|s| *s.borrow_mut() = Some(at.into()));
        past_grace(&w);
        for _ in 0..4 {
            pass(&w).await;
            past_grace(&w);
        }
        assert!(!hidden_path(&w).exists(), "{at}/{phase}");
        if phase == "place" {
            assert_eq!(read(&w, "a.segy"), OLD, "{at}/{phase}");
            assert_eq!(read(&w, "a-restored.segy"), body, "{at}/{phase}");
        } else {
            assert_eq!(read(&w, "a.segy"), body, "{at}/{phase}");
            assert_eq!(read(&w, &aside(&w).unwrap()), OLD, "{at}/{phase}");
        }
        assert_eq!(state(&w, 1).state, "done", "{at}/{phase}");
    }
}

#[test]
fn the_kept_aside_name_is_visible_and_never_restored() {
    let at = 1_762_862_400_000; // 2025-11-11T12:00:00Z
    assert_eq!(
        replace::aside_name("a.segy", at, 0),
        "a-replaced-2025-11-11-120000Z.segy"
    );
    assert_eq!(
        replace::aside_name("a.segy", at, 1),
        "a-replaced-2025-11-11-120000Z-2.segy"
    );
    assert_eq!(
        replace::aside_name(".bashrc", at, 0),
        ".bashrc-replaced-2025-11-11-120000Z"
    );
    let long = "x".repeat(300);
    assert!(replace::aside_name(&long, at, 0).len() <= 255);
    assert!(!replace::aside_name("a.segy", at, 0).contains("restored"));
}

#[tokio::test]
async fn a_scan_while_the_old_file_is_hidden_reports_nothing_of_it() {
    let body = data(1000);
    let w = replace_world(&body);
    let lts = w.nas.path().join("lts");
    let mut s = crate::annex::scan::Scanner::new(&w.db, &lts, w.now.get(), true);
    s.full().unwrap();
    let seen = w.db.outbox(1000).unwrap().len();
    pass(&w).await;
    assert!(hidden_path(&w).exists() && !old_path(&w).exists());
    w.now.set(w.now.get() + 1000);
    let mut s = crate::annex::scan::Scanner::new(&w.db, &lts, w.now.get(), false);
    s.full().unwrap();
    let after = w.db.outbox(1000).unwrap();
    let new: Vec<_> = after[seen..]
        .iter()
        .map(|(_, k, p, _)| format!("{k} {}", p.clone().unwrap_or_default()))
        .collect();
    assert!(
        new.iter()
            .all(|e| !e.contains("a.segy") && !e.contains(".omnifile-replacing-")),
        "{new:?}"
    );
    assert!(
        w.db.entry("surveys/a.segy").unwrap().is_some(),
        "still the annex's file"
    );
}

#[tokio::test]
async fn a_change_while_the_service_is_asked_is_seen_before_the_hide() {
    let body = data(1000);
    let w = replace_world(&body);
    let p = old_path(&w);
    replace::faults::at("after_replace_go", move || {
        use std::io::Write;
        std::fs::OpenOptions::new()
            .append(true)
            .open(&p)
            .unwrap()
            .write_all(b"?")
            .unwrap();
    });
    pass(&w).await;
    assert!(!hidden_path(&w).exists(), "never hidden");
    assert_eq!(read(&w, "a-restored.segy"), body);
    assert_eq!(asked(&w, "replace_put_back")[0]["reason"], "changed");
}

#[tokio::test]
async fn a_staged_file_changed_in_the_grace_is_never_linked_and_the_old_one_goes_back() {
    let body = data(1000);
    let w = replace_world(&body);
    pass(&w).await;
    let staged = staging(&w).join(&state(&w, 1).staging_name);
    {
        use std::io::Write;
        std::fs::OpenOptions::new()
            .append(true)
            .open(&staged)
            .unwrap()
            .write_all(b"tampered")
            .unwrap();
    }
    past_grace(&w);
    pass(&w).await;
    let p = state(&w, 1);
    assert_eq!(
        (p.state.as_str(), p.reason.as_deref()),
        ("failed", Some("staging_file_changed"))
    );
    assert_eq!(read(&w, "a.segy"), OLD, "back under its own name");
    assert!(!hidden_path(&w).exists());
    assert_eq!(listing(&w), ["a.segy"], "nothing placed");
}

#[tokio::test]
async fn the_service_hears_the_hide_before_it_is_asked_about_it() {
    let body = data(1000);
    let w = replace_world(&body);
    // The report after the hide doesn't reach the service.
    replace::faults::REPORT_FAILS.with(|x| x.set(1));
    pass(&w).await;
    assert!(asked(&w, "replace_hidden").is_empty());
    past_grace(&w);
    pass(&w).await;
    assert_eq!(state(&w, 1).outcome.as_deref(), Some("replaced"));
    let order: Vec<String> = w
        .svc
        .state
        .lock()
        .unwrap()
        .placement_steps
        .iter()
        .map(|(_, b)| b["step"].as_str().unwrap_or("").to_owned())
        .filter(|s| s.starts_with("replace"))
        .collect();
    assert_eq!(
        order[..3],
        ["replace_go", "replace_hidden", "replace_delete_go"]
    );
}

// --- The confirm pass of #98 ------------------------------------------------------------

/// C1: a user renames the folder the old file is hidden in and makes a new one of
/// that name. The hidden file is found by its inode and goes back under its own name
/// in its folder; never reported gone. Also after a restart before the hide's journal.
#[tokio::test]
async fn a_hidden_file_whose_folder_moved_goes_back_there() {
    for stop in [None, Some("replace_w1_taken")] {
        let body = data(1000);
        let w = replace_world(&body);
        if let Some(s) = stop {
            faults::STOP_AT.with(|x| *x.borrow_mut() = Some(s.into()));
        }
        pass(&w).await;
        let lts = w.nas.path().join("lts");
        std::fs::rename(lts.join("surveys"), lts.join("surveys-old")).unwrap();
        std::fs::create_dir(lts.join("surveys")).unwrap();
        std::fs::set_permissions(lts.join("surveys"), std::fs::Permissions::from_mode(0o775))
            .unwrap();
        for _ in 0..3 {
            past_grace(&w);
            pass(&w).await;
        }
        assert_eq!(
            std::fs::read(lts.join("surveys-old/a.segy")).unwrap(),
            OLD,
            "{stop:?}: back under its name, in its folder"
        );
        assert!(
            !lts.join("surveys-old")
                .join(replace::hidden_name(1, "a.segy"))
                .exists(),
            "{stop:?}: never left hidden"
        );
        assert!(
            asked(&w, "replace_gone").is_empty(),
            "{stop:?}: never said gone"
        );
        assert_eq!(asked(&w, "replace_put_back")[0]["reason"], "folder_moved");
        assert!(asked(&w, "replaced").is_empty());
        assert_eq!(read(&w, "a-restored.segy"), body, "{stop:?}");
    }
}

/// C1: the folder the old file was hidden in is gone and the file isn't found
/// anywhere: `stuck` naming the hidden file, never reported gone.
#[tokio::test]
async fn a_hidden_file_whose_folder_cant_be_found_is_stuck_not_gone() {
    let body = data(1000);
    let w = replace_world(&body);
    pass(&w).await;
    let lts = w.nas.path().join("lts");
    std::fs::rename(lts.join("surveys"), w.nas.path().join("elsewhere")).unwrap();
    std::fs::remove_dir_all(w.nas.path().join("elsewhere")).unwrap();
    std::fs::create_dir(lts.join("surveys")).unwrap();
    past_grace(&w);
    pass(&w).await;
    let p = state(&w, 1);
    assert_eq!(p.state, "stuck", "{p:?}");
    assert!(
        p.reason
            .unwrap_or_default()
            .contains(".omnifile-replacing-1-a.segy")
    );
    assert!(asked(&w, "replace_gone").is_empty());
}

/// C2: a restart in `deleting` with a user's file under our hidden name: it is kept
/// aside with a Problem, never taken as deleted and left hidden.
#[tokio::test]
async fn a_users_file_at_the_hidden_name_after_a_crash_in_deleting_is_kept() {
    let body = data(1000);
    let w = replace_world(&body);
    pass(&w).await;
    faults::STOP_AT.with(|x| *x.borrow_mut() = Some("replace_deleting".into()));
    past_grace(&w);
    pass(&w).await;
    assert_eq!(row(&w).state, "deleting");
    let h = hidden_path(&w);
    let tmp = h.with_extension("tmp");
    std::fs::write(&tmp, b"a user's file").unwrap();
    std::fs::rename(&tmp, &h).unwrap();
    pass(&w).await;
    assert!(!hidden_path(&w).exists(), "never left hidden");
    let kept = aside(&w).expect("kept aside");
    assert_eq!(read(&w, &kept), b"a user's file");
    assert_eq!(read(&w, "a.segy"), body);
    assert!(asked(&w, "replaced").is_empty(), "not taken as deleted");
    assert_eq!(asked(&w, "replace_aside")[0]["reason"], "swapped_at_delete");
}

/// C4: our W3 placeholder at the name, then the user takes the hidden file away:
/// the placeholder is removed, never left squatting on the name.
#[tokio::test]
async fn a_placeholder_never_squats_on_the_name_when_the_old_file_went() {
    let body = data(1000);
    let w = replace_world(&body);
    pass(&w).await;
    let dir = open_root(w.nas.path(), "")
        .unwrap()
        .sub("lts/surveys")
        .unwrap();
    let (pino, pctime) = crate::annex::remover::imp::make_placeholder(&dir, "a.segy").unwrap();
    w.db.conn()
        .execute(
            "UPDATE placement_replace SET placeholder_ino = ?1, placeholder_ctime = ?2, \
             placeholder_name = 'a.segy' WHERE placement_id = 1",
            rusqlite::params![pino as i64, pctime],
        )
        .unwrap();
    std::fs::remove_file(hidden_path(&w)).unwrap();
    past_grace(&w);
    pass(&w).await;
    assert_eq!(read(&w, "a.segy"), body, "the name is the new file's");
    assert_eq!(listing(&w), ["a.segy"], "no placeholder left");
    assert_eq!(asked(&w, "replace_gone").len(), 1);
}

/// C5: W3 kept the old file aside but its reply was lost, then the placement ended:
/// it is reported kept aside (a Problem names it), never gone.
#[tokio::test]
async fn an_aside_whose_reply_was_lost_is_reported_at_the_end() {
    let body = data(1000);
    let w = replace_world(&body);
    pass(&w).await;
    let kept = "a-replaced-2026-11-12-120000Z.segy";
    std::fs::rename(hidden_path(&w), w.nas.path().join("lts/surveys").join(kept)).unwrap();
    w.db.conn()
        .execute(
            "UPDATE placement_replace SET state = 'putting_aside', aside_name = ?1 \
             WHERE placement_id = 1",
            [kept],
        )
        .unwrap();
    let p = state(&w, 1);
    assert!(with_placer(&w, |pl| pl.unwind_old(&p)).is_none());
    assert_eq!(row(&w).state, "put_aside");
    assert_eq!(read(&w, kept), OLD);
    let unreported: String =
        w.db.conn()
            .query_row(
                "SELECT unreported FROM placement_replace WHERE placement_id = 1",
                [],
                |r| r.get(0),
            )
            .unwrap();
    assert!(
        unreported.ends_with("aside") && !unreported.contains("gone"),
        "{unreported}"
    );
}

/// C6: the state and its fact are one write. A trigger refuses a fact written on
/// its own after the state (as a crash between two writes would lose it).
#[tokio::test]
async fn a_replace_gone_fact_is_written_with_its_state() {
    let body = data(1000);
    let w = replace_world(&body);
    pass(&w).await;
    w.db.conn()
        .execute_batch(
            "CREATE TRIGGER lose_late_fact BEFORE UPDATE OF unreported ON placement_replace \
             WHEN NEW.state = OLD.state AND NEW.state = 'old_gone' \
             BEGIN SELECT RAISE(ABORT, 'crashed'); END;",
        )
        .unwrap();
    std::fs::remove_file(hidden_path(&w)).unwrap();
    past_grace(&w);
    pass(&w).await;
    assert!(
        !asked(&w, "replace_gone").is_empty(),
        "the fact wasn't lost"
    );
}
