//! Users' renames, moves, deletes and owner changes reach the service
//! (docs/design/namespace-sync.md): real xfs, the daemon's own fanotify
//! watcher, the fake service. Run as root.
#![cfg(target_os = "linux")]

use std::path::{Path, PathBuf};
use std::sync::Arc;
use std::time::{Duration, Instant};

use omnifile_edge::checks::startup_checks;
use omnifile_edge::config::Config;
use omnifile_edge::daemon::Daemon;
use omnifile_edge::fakes::{FakeService, events};

fn base() -> Option<PathBuf> {
    // SAFETY: geteuid has no preconditions.
    if unsafe { libc::geteuid() } != 0 {
        eprintln!("skipping: needs root");
        return None;
    }
    let b = PathBuf::from(
        std::env::var("OMNIFILE_TEST_DIR").unwrap_or_else(|_| "/srv/omnifile/data/test".into()),
    );
    std::fs::create_dir_all(&b).ok()?;
    if omnifile_stub::sys::fanotify_guarded(&b).unwrap_or(false) {
        eprintln!(
            "skipping: {} is watched by a running smb-guard",
            b.display()
        );
        return None;
    }
    Some(b)
}

fn config(dir: &Path, extra: &str) -> Config {
    config_exe(dir, extra, &std::env::current_exe().unwrap())
}

/// `exe` stands for Ganesha's binary (`[fsal] required_exe`): its processes'
/// changes come with no hint of their own, as Ganesha's do.
fn config_exe(dir: &Path, extra: &str, exe: &Path) -> Config {
    let toml = format!(
        r#"
        data_root = "{d}/data"
        replica_path = "{d}/state/replica.db"
        socket_path = "{d}/run/fsal.sock"
        edge_id = 7
        backend = "fake"
        [fsal]
        required_exe = "{exe}"
        # No FSAL connects in these tests: commits would otherwise wait for
        # one to say no NFSv4 client holds the file open (#54).
        no_ganesha = true
        [feed]
        wait_s = 1
        retry_s = 1
        reconcile_s = 0
        [commit]
        detect = false
        # Writes handed to the commit by hand wait out this period (#80 review).
        quiet_s = 1
        [namespace]
        quiet_ms = 200
        {extra}
        [log]
        format = "text"
        filter = "debug"
        "#,
        d = dir.display(),
        exe = exe.display()
    );
    std::fs::create_dir_all(dir.join("data")).unwrap();
    Config::from_toml(&toml, Path::new("test.toml")).unwrap()
}

async fn eventually(what: &str, mut f: impl FnMut() -> bool) {
    let deadline = Instant::now() + Duration::from_secs(15);
    while !f() {
        assert!(Instant::now() < deadline, "timed out: {what}");
        tokio::time::sleep(Duration::from_millis(50)).await;
    }
}

/// Change the tree as a user would: from another process (the daemon skips
/// its own changes, and this test runs in the daemon's process).
fn sh(args: &[&str]) {
    let st = std::process::Command::new(args[0])
        .args(&args[1..])
        .status()
        .unwrap();
    assert!(st.success(), "{args:?}");
}

fn p(root: &Path, rel: &str) -> String {
    root.join(rel).display().to_string()
}

fn has(svc: &FakeService, call: &str) -> bool {
    svc.calls().iter().any(|c| c == call)
}

fn pos(svc: &FakeService, call: &str) -> usize {
    svc.calls()
        .iter()
        .position(|c| c == call)
        .unwrap_or_else(|| panic!("no call {call:?} in {:?}", svc.calls()))
}

/// A dataset root `proj` (100) with a folder `a` (101) holding `f.bin`
/// (102), and `keep.bin` (103) at the root, materialised as placeholders.
async fn world(
    dir: &Path,
    extra: &str,
) -> (Daemon<FakeService, FakeService>, Arc<FakeService>, PathBuf) {
    world_cfg(config(dir, extra)).await
}

async fn world_cfg(cfg: Config) -> (Daemon<FakeService, FakeService>, Arc<FakeService>, PathBuf) {
    omnifile_edge::init_logging("text", "debug");
    let vol = startup_checks(&cfg).unwrap();
    let svc = Arc::new(FakeService::new());
    let root = cfg.data_root.join("proj");
    let d = Daemon::start(cfg, vol, svc.clone(), svc.clone())
        .await
        .unwrap();
    svc.push_event(events::share(1, 100, "proj", "fail", 1));
    svc.push_event(events::dir(100, None, "", 1));
    svc.push_event(events::dir(101, Some(100), "a", 1));
    for (id, parent, name) in [(102, 101, "f.bin"), (103, 100, "keep.bin")] {
        svc.push_event(events::file(
            id,
            parent,
            name,
            10,
            id * 10,
            Some(1),
            false,
            None,
            1,
        ));
    }
    let (f, k) = (root.join("a/f.bin"), root.join("keep.bin"));
    eventually("the tree materialised", || f.exists() && k.exists()).await;
    (d, svc, root)
}

#[tokio::test(flavor = "multi_thread", worker_threads = 4)]
async fn renames_moves_new_folders_chowns_and_deletes_reach_the_service() {
    let Some(base) = base() else { return };
    let dir = tempfile::tempdir_in(&base).unwrap();
    let (d, svc, root) = world(dir.path(), "sweep_at_start = false").await;

    // A rename in place.
    sh(&["mv", &p(&root, "a/f.bin"), &p(&root, "a/g.bin")]);
    eventually("rename sent", || has(&svc, "rename 102 101 g.bin")).await;
    // A move to another folder.
    sh(&["mv", &p(&root, "keep.bin"), &p(&root, "a/keep.bin")]);
    eventually("move sent", || has(&svc, "rename 103 101 keep.bin")).await;
    // A new, empty folder is created in the catalog.
    sh(&["mkdir", &p(&root, "new")]);
    eventually("new folder created", || has(&svc, "create 100 new")).await;
    // A folder renamed: one op, its contents follow.
    sh(&["mv", &p(&root, "a"), &p(&root, "b")]);
    eventually("folder rename sent", || has(&svc, "rename 101 100 b")).await;
    // chown.
    sh(&["chown", "4321:4322", &p(&root, "b/g.bin")]);
    eventually("owner sent", || has(&svc, "owner 102 4321 4322")).await;

    // The materialiser never moves anything back.
    tokio::time::sleep(Duration::from_secs(2)).await;
    assert!(root.join("b/g.bin").exists() && root.join("b/keep.bin").exists());
    assert!(!root.join("a").exists() && !root.join("keep.bin").exists());

    // A folder deleted with what's in it: children first, then the folder.
    sh(&["rm", "-r", &p(&root, "b")]);
    eventually("deletes sent", || has(&svc, "unlink 101")).await;
    assert!(pos(&svc, "unlink 102") < pos(&svc, "unlink 101"));
    assert!(pos(&svc, "unlink 103") < pos(&svc, "unlink 101"));
    // And they stay deleted.
    tokio::time::sleep(Duration::from_secs(2)).await;
    assert!(!root.join("b").exists());
    let n = svc.calls().len();
    tokio::time::sleep(Duration::from_secs(2)).await;
    assert!(
        svc.calls()[n..]
            .iter()
            .all(|c| !c.starts_with("rename") && !c.starts_with("unlink")),
        "nothing is sent twice: {:?}",
        &svc.calls()[n..]
    );
    d.shutdown().await;
}

#[tokio::test(flavor = "multi_thread", worker_threads = 4)]
async fn changes_made_while_the_daemon_was_down_are_found_by_the_sweep() {
    let Some(base) = base() else { return };
    let dir = tempfile::tempdir_in(&base).unwrap();
    let (d, svc, root) = world(dir.path(), "sweep_at_start = false").await;
    d.shutdown().await;
    // While it's down: a rename and a delete.
    std::fs::rename(root.join("a/f.bin"), root.join("a/h.bin")).unwrap();
    std::fs::remove_file(root.join("keep.bin")).unwrap();
    let cfg = config(dir.path(), "sweep_at_start = true");
    let vol = startup_checks(&cfg).unwrap();
    let d = Daemon::start(cfg, vol, svc.clone(), svc.clone())
        .await
        .unwrap();
    eventually("the sweep found both", || {
        has(&svc, "rename 102 101 h.bin") && has(&svc, "unlink 103")
    })
    .await;
    d.shutdown().await;
}

#[tokio::test(flavor = "multi_thread", worker_threads = 4)]
async fn a_refused_rename_keeps_the_users_tree_and_is_not_retried() {
    let Some(base) = base() else { return };
    let dir = tempfile::tempdir_in(&base).unwrap();
    let (d, svc, root) = world(dir.path(), "sweep_at_start = false").await;
    svc.conflict_on_rename_to("taken.bin");
    sh(&["mv", &p(&root, "keep.bin"), &p(&root, "taken.bin")]);
    eventually("rename sent", || has(&svc, "rename 103 100 taken.bin")).await;
    tokio::time::sleep(Duration::from_secs(3)).await;
    assert!(root.join("taken.bin").exists(), "the user's name stays");
    let sent = svc
        .calls()
        .iter()
        .filter(|c| *c == "rename 103 100 taken.bin")
        .count();
    assert_eq!(sent, 1, "a refused op isn't sent again");
    d.shutdown().await;
}

#[tokio::test(flavor = "multi_thread", worker_threads = 4)]
async fn ops_wait_while_the_service_is_away_and_go_when_it_is_back() {
    let Some(base) = base() else { return };
    let dir = tempfile::tempdir_in(&base).unwrap();
    let (d, svc, root) = world(dir.path(), "sweep_at_start = false").await;
    svc.set_unavailable(true);
    sh(&["mv", &p(&root, "keep.bin"), &p(&root, "later.bin")]);
    tokio::time::sleep(Duration::from_secs(2)).await;
    assert!(!has(&svc, "rename 103 100 later.bin"));
    svc.set_unavailable(false);
    eventually("sent once the service is back", || {
        has(&svc, "rename 103 100 later.bin")
    })
    .await;
    d.shutdown().await;
}

/// The catalog as the service would have it after `calls`, starting from
/// `world`'s names. Panics if a rename ever names a taken name: the real
/// service answers that with `conflict`.
fn replay(calls: &[String]) -> std::collections::HashMap<(u64, String), u64> {
    let mut names: std::collections::HashMap<(u64, String), u64> = [
        ((100, "a".to_string()), 101),
        ((101, "f.bin".to_string()), 102),
        ((100, "keep.bin".to_string()), 103),
    ]
    .into_iter()
    .collect();
    for c in calls {
        let w: Vec<&str> = c.split(' ').collect();
        match w[..] {
            ["rename", f, parent, name] => {
                let (f, parent): (u64, u64) = (f.parse().unwrap(), parent.parse().unwrap());
                let key = (parent, name.to_string());
                if let Some(other) = names.get(&key) {
                    assert_eq!(*other, f, "{c}: the name is taken (calls: {calls:?})");
                }
                names.retain(|_, v| *v != f);
                names.insert(key, f);
            }
            ["unlink", f] => {
                let f: u64 = f.parse().unwrap();
                names.retain(|_, v| *v != f);
            }
            _ => {}
        }
    }
    names
}

#[tokio::test(flavor = "multi_thread", worker_threads = 4)]
async fn swapped_names_never_send_a_rename_onto_a_taken_name() {
    let Some(base) = base() else { return };
    let dir = tempfile::tempdir_in(&base).unwrap();
    let (d, svc, root) = world(dir.path(), "sweep_at_start = false").await;
    let at = |names: &std::collections::HashMap<(u64, String), u64>, p: u64, n: &str| {
        names.get(&(p, n.to_string())).copied()
    };
    // In one folder: the folder `a` and the file `keep.bin` swap names.
    let r = root.display().to_string();
    sh(&[
        "sh",
        "-c",
        &format!("cd {r} && mv keep.bin t && mv a keep.bin && mv t a"),
    ]);
    eventually("the swap reached the service", || {
        let n = replay(&svc.calls());
        at(&n, 100, "keep.bin") == Some(101) && at(&n, 100, "a") == Some(103)
    })
    .await;
    // Across folders: the file `a` and `keep.bin/f.bin` swap places.
    sh(&[
        "sh",
        "-c",
        &format!("cd {r} && mv a t && mv keep.bin/f.bin a && mv t keep.bin/f.bin"),
    ]);
    eventually("the second swap reached the service", || {
        let n = replay(&svc.calls());
        at(&n, 100, "a") == Some(102) && at(&n, 101, "f.bin") == Some(103)
    })
    .await;
    // Nothing is left under a temporary name, and the tree is the user's.
    let n = replay(&svc.calls());
    assert!(
        n.keys().all(|(_, name)| !name.starts_with(".omnifile-mv-")),
        "{n:?}"
    );
    tokio::time::sleep(Duration::from_secs(2)).await;
    assert!(root.join("keep.bin/f.bin").is_file() && root.join("a").is_file());
    d.shutdown().await;
}

fn never(svc: &FakeService, call: &str) -> bool {
    !has(svc, call)
}

/// M7 review B1: a folder emptied by moves and then removed, all within one
/// quiet period, never tombstones what moved out of it.
#[tokio::test(flavor = "multi_thread", worker_threads = 4)]
async fn a_folder_emptied_by_moves_then_removed_unlinks_only_the_folder() {
    let Some(base) = base() else { return };
    let dir = tempfile::tempdir_in(&base).unwrap();
    let (d, svc, root) = world(dir.path(), "sweep_at_start = false").await;
    let r = root.display().to_string();
    // Moved up into the parent, then the folder removed.
    sh(&["sh", "-c", &format!("cd {r} && mv a/f.bin . && rmdir a")]);
    eventually("the folder's delete was sent", || has(&svc, "unlink 101")).await;
    assert!(has(&svc, "rename 102 100 f.bin"), "{:?}", svc.calls());
    assert!(pos(&svc, "rename 102 100 f.bin") < pos(&svc, "unlink 101"));
    tokio::time::sleep(Duration::from_secs(2)).await;
    assert!(
        never(&svc, "unlink 102"),
        "a live file was unlinked: {:?}",
        svc.calls()
    );
    d.shutdown().await;
}

/// The same, with the file moved into a new folder the parent's pass sees
/// only later (its lid is higher): the folder's delete waits for that move.
#[tokio::test(flavor = "multi_thread", worker_threads = 4)]
async fn a_folder_emptied_into_a_new_folder_waits_for_the_move() {
    let Some(base) = base() else { return };
    let dir = tempfile::tempdir_in(&base).unwrap();
    let (d, svc, root) = world(dir.path(), "sweep_at_start = false").await;
    let r = root.display().to_string();
    sh(&[
        "sh",
        "-c",
        &format!("cd {r} && mkdir z && mv a/f.bin z/ && rmdir a"),
    ]);
    eventually("the folder's delete was sent", || has(&svc, "unlink 101")).await;
    let moved = svc
        .calls()
        .iter()
        .position(|c| c.starts_with("rename 102 ") && c.ends_with(" f.bin"))
        .unwrap_or_else(|| panic!("no rename of f.bin: {:?}", svc.calls()));
    assert!(moved < pos(&svc, "unlink 101"));
    tokio::time::sleep(Duration::from_secs(2)).await;
    assert!(
        never(&svc, "unlink 102"),
        "a live file was unlinked: {:?}",
        svc.calls()
    );
    d.shutdown().await;
}

/// M7 review B2: a folder created while the service was away, then removed,
/// never holds the namespace queue.
#[tokio::test(flavor = "multi_thread", worker_threads = 4)]
async fn a_create_for_something_already_gone_does_not_hold_the_queue() {
    let Some(base) = base() else { return };
    let dir = tempfile::tempdir_in(&base).unwrap();
    let (d, svc, root) = world(dir.path(), "sweep_at_start = false\ncreate_resend_s = 1").await;
    svc.set_unavailable(true);
    sh(&["mkdir", &p(&root, "tmpdir")]);
    tokio::time::sleep(Duration::from_secs(2)).await;
    sh(&["rmdir", &p(&root, "tmpdir")]);
    tokio::time::sleep(Duration::from_secs(2)).await;
    svc.set_unavailable(false);
    sh(&["mv", &p(&root, "keep.bin"), &p(&root, "after.bin")]);
    eventually("later ops still go", || {
        has(&svc, "rename 103 100 after.bin")
    })
    .await;
    // And again later: the queue isn't stuck behind the dead create.
    tokio::time::sleep(Duration::from_secs(3)).await;
    sh(&["mv", &p(&root, "after.bin"), &p(&root, "later.bin")]);
    eventually("and the next", || has(&svc, "rename 103 100 later.bin")).await;
    d.shutdown().await;
}

/// M7 review B3: a name whose inode number came back for a new file (`rm`
/// then a new file of the same name) is not the old file: the old one is
/// unlinked. xfs usually hands the freed inode number to the next file;
/// the test tries until it does.
#[tokio::test(flavor = "multi_thread", worker_threads = 4)]
async fn a_reused_inode_number_is_not_the_old_file() {
    use std::os::unix::fs::MetadataExt;
    let Some(base) = base() else { return };
    let dir = tempfile::tempdir_in(&base).unwrap();
    let (d, svc, root) = world(dir.path(), "sweep_at_start = false").await;
    let f = root.join("a/f.bin");
    let old = std::fs::metadata(&f).unwrap().ino();
    let r = root.display().to_string();
    let mut reused = false;
    for _ in 0..20 {
        sh(&[
            "sh",
            "-c",
            &format!("cd {r}/a && rm f.bin && echo new > f.bin"),
        ]);
        if std::fs::metadata(&f).unwrap().ino() == old {
            reused = true;
            break;
        }
    }
    if !reused {
        eprintln!("skipping: xfs didn't reuse the inode number");
        d.shutdown().await;
        return;
    }
    eventually("the old file's delete was sent", || has(&svc, "unlink 102")).await;
    d.shutdown().await;
}

/// M7 review S1: a second link to a file is not a rename.
#[tokio::test(flavor = "multi_thread", worker_threads = 4)]
async fn a_hard_link_is_not_a_rename() {
    let Some(base) = base() else { return };
    let dir = tempfile::tempdir_in(&base).unwrap();
    let (d, svc, root) = world(dir.path(), "sweep_at_start = false").await;
    sh(&["ln", &p(&root, "a/f.bin"), &p(&root, "link.bin")]);
    // Something else in the same pass, to know the pass ran.
    sh(&["mv", &p(&root, "keep.bin"), &p(&root, "kept.bin")]);
    eventually("the pass ran", || has(&svc, "rename 103 100 kept.bin")).await;
    tokio::time::sleep(Duration::from_secs(2)).await;
    assert!(
        svc.calls().iter().all(|c| !c.starts_with("rename 102 ")),
        "a link was taken for a rename: {:?}",
        svc.calls()
    );
    d.shutdown().await;
}

/// M7 review S2: a name ending in " (deleted)" is a name, not a delete.
#[tokio::test(flavor = "multi_thread", worker_threads = 4)]
async fn a_name_ending_in_deleted_is_a_move() {
    let Some(base) = base() else { return };
    let dir = tempfile::tempdir_in(&base).unwrap();
    let (d, svc, root) = world(dir.path(), "sweep_at_start = false").await;
    sh(&["mkdir", &p(&root, "z")]);
    eventually("z created", || has(&svc, "create 100 z")).await;
    tokio::time::sleep(Duration::from_secs(1)).await;
    sh(&["mv", &p(&root, "a/f.bin"), &p(&root, "z/f (deleted)")]);
    eventually("the move was sent", || {
        svc.calls()
            .iter()
            .any(|c| c.starts_with("rename 102 ") && c.ends_with(" f (deleted)"))
    })
    .await;
    assert!(never(&svc, "unlink 102"), "{:?}", svc.calls());
    d.shutdown().await;
}

/// M7 review N1: `mkdir -p` puts every level in the catalog, not only the
/// first, without waiting for the daily sweep.
#[tokio::test(flavor = "multi_thread", worker_threads = 4)]
async fn nested_new_folders_all_reach_the_catalog() {
    let Some(base) = base() else { return };
    let dir = tempfile::tempdir_in(&base).unwrap();
    let (d, svc, root) = world(dir.path(), "sweep_at_start = false").await;
    sh(&["mkdir", "-p", &p(&root, "n1/n2/n3")]);
    eventually("all three levels created", || {
        let c = svc.calls();
        ["n1", "n2", "n3"].iter().all(|n| {
            c.iter()
                .any(|x| x.starts_with("create ") && x.ends_with(&format!(" {n}")))
        })
    })
    .await;
    d.shutdown().await;
}

/// M7 re-review R1: a folder the service refuses (its name taken in the
/// catalog), with a file then moved into it, holds nothing behind it.
#[tokio::test(flavor = "multi_thread", worker_threads = 4)]
async fn a_refused_folder_with_a_file_moved_in_holds_nothing_behind_it() {
    let Some(base) = base() else { return };
    let dir = tempfile::tempdir_in(&base).unwrap();
    let (d, svc, root) = world(dir.path(), "sweep_at_start = false\ncreate_resend_s = 1").await;
    svc.conflict_on_create_named("Reports");
    sh(&["mkdir", &p(&root, "Reports")]);
    eventually("its create was refused", || has(&svc, "create 100 Reports")).await;
    tokio::time::sleep(Duration::from_millis(500)).await;
    sh(&["mv", &p(&root, "keep.bin"), &p(&root, "Reports/keep.bin")]);
    tokio::time::sleep(Duration::from_secs(3)).await;
    sh(&["mv", &p(&root, "a/f.bin"), &p(&root, "a/g.bin")]);
    eventually("the ops behind it still go", || {
        has(&svc, "rename 102 101 g.bin")
    })
    .await;
    d.shutdown().await;
}

/// M7 re-review R2: a folder whose create was made but whose answer was
/// lost, removed before the create was sent again, is removed from the
/// catalog too (never left there for a re-snapshot to bring back).
#[tokio::test(flavor = "multi_thread", worker_threads = 4)]
async fn a_create_whose_answer_was_lost_then_removed_is_unlinked() {
    let Some(base) = base() else { return };
    let dir = tempfile::tempdir_in(&base).unwrap();
    let (d, svc, root) = world(dir.path(), "sweep_at_start = false\ncreate_resend_s = 1").await;
    svc.lose_next_create();
    sh(&["mkdir", &p(&root, "lost")]);
    eventually("its create was sent", || has(&svc, "create 100 lost")).await;
    sh(&["rmdir", &p(&root, "lost")]);
    eventually("the folder the service made is unlinked", || {
        svc.calls().iter().any(|c| {
            c.strip_prefix("unlink ")
                .is_some_and(|id| !["101", "102", "103"].contains(&id))
        })
    })
    .await;
    d.shutdown().await;
}

// --- who made it (control-api.md 4.1 `actor`) and hard links (decision 42) ---

use omnifile_edge::ports::{Actor, ActorVia, CloseWrite};
use omnifile_fsal_proto::wire::{Identity, NsHint, ObjRef};

/// python3's real binary: it stands for Ganesha in the tests that hint NFS
/// callers, so its own changes come with no hint (as Ganesha's).
fn python_exe() -> PathBuf {
    let out = std::process::Command::new("sh")
        .args(["-c", "command -v python3"])
        .output()
        .unwrap();
    std::fs::canonicalize(String::from_utf8(out.stdout).unwrap().trim()).unwrap()
}

/// Run Python (a process that stays alive `linger` seconds after its change,
/// so its uid can still be read).
fn py(code: &str, linger: u32) {
    let st = std::process::Command::new("python3")
        .args([
            "-c",
            &format!("import os, time\n{code}\ntime.sleep({linger})"),
        ])
        .status()
        .unwrap();
    assert!(st.success(), "{code}");
}

async fn key_of(d: &Daemon<FakeService, FakeService>, file_id: u64) -> ObjRef {
    let n = d
        .core
        .replica
        .lock()
        .await
        .node_by_file_id(file_id)
        .unwrap()
        .unwrap();
    ObjRef {
        ino: n.ino.unwrap(),
        ino_gen: n.ino_gen.unwrap(),
        ..ObjRef::default()
    }
}

fn caller(uid: u32, sec: u8) -> Identity {
    let mut id = Identity {
        uid,
        gid: 10000,
        sec,
        nfs_vers: 4,
        nfs_minor: 2,
        addr_family: 4,
        ..Identity::default()
    };
    id.addr[..4].copy_from_slice(&[10, 0, 0, 7]);
    id
}

fn hint(
    op: u8,
    what: u8,
    parent: ObjRef,
    target: ObjRef,
    id: Identity,
    name: &str,
    new: &str,
) -> NsHint {
    NsHint {
        op,
        what,
        parent,
        target,
        identity: id,
        principal: Vec::new(),
        name: name.as_bytes().to_vec(),
        new_name: new.as_bytes().to_vec(),
    }
}

fn actor(svc: &FakeService, call: &str) -> Actor {
    svc.actor_of(call)
        .unwrap_or_else(|| panic!("no {call:?} in {:?}", svc.calls()))
        .unwrap_or_else(|| panic!("{call:?} was sent with no actor"))
}

/// NFS callers (the FSAL's NS_HINT) name each op; an op nobody hinted, from
/// Ganesha's process, is `unknown`, never Ganesha's own (root) uid.
#[tokio::test(flavor = "multi_thread", worker_threads = 4)]
async fn nfs_callers_are_named_and_unhinted_changes_are_unknown() {
    let Some(base) = base() else { return };
    let dir = tempfile::tempdir_in(&base).unwrap();
    let cfg = config_exe(dir.path(), "sweep_at_start = false", &python_exe());
    let (d, svc, root) = world_cfg(cfg).await;
    let (f, a, top) = (
        key_of(&d, 102).await,
        key_of(&d, 101).await,
        key_of(&d, 100).await,
    );
    let r = root.display().to_string();

    // A rename by uid 20002 over sec=sys: claimed, with its address.
    d.core
        .ns_hint_nfs(&hint(3, 0, a, f, caller(20002, 1), "f.bin", "g.bin"));
    py(&format!("os.rename('{r}/a/f.bin', '{r}/a/g.bin')"), 0);
    eventually("rename sent", || has(&svc, "rename 102 101 g.bin")).await;
    let x = actor(&svc, "rename 102 101 g.bin");
    assert_eq!(
        (x.via, x.uid, x.sec, x.proof, x.client_addr.as_deref()),
        (
            ActorVia::Nfs,
            Some(20002),
            Some(omnifile_control::types::Sec::Sys),
            Some(omnifile_control::types::Proof::Claimed),
            Some("10.0.0.7")
        )
    );
    // An owner change over krb5p: proven (the service decides in the end).
    d.core
        .ns_hint_nfs(&hint(4, 2, ObjRef::default(), f, caller(20003, 4), "", ""));
    py(&format!("os.chown('{r}/a/g.bin', 4321, 4322)"), 0);
    eventually("owner sent", || has(&svc, "owner 102 4321 4322")).await;
    let x = actor(&svc, "owner 102 4321 4322");
    assert_eq!((x.via, x.uid), (ActorVia::Nfs, Some(20003)));
    assert_eq!(x.proof, Some(omnifile_control::types::Proof::Proven));
    // A new folder.
    d.core.ns_hint_nfs(&hint(
        7,
        0,
        top,
        ObjRef::default(),
        caller(20004, 1),
        "newd",
        "",
    ));
    py(&format!("os.mkdir('{r}/newd')"), 0);
    eventually("folder created", || has(&svc, "create 100 newd")).await;
    assert_eq!(actor(&svc, "create 100 newd").uid, Some(20004));
    // A delete.
    d.core
        .ns_hint_nfs(&hint(2, 0, a, f, caller(20005, 1), "g.bin", ""));
    py(&format!("os.remove('{r}/a/g.bin')"), 0);
    eventually("unlink sent", || has(&svc, "unlink 102")).await;
    assert_eq!(actor(&svc, "unlink 102").uid, Some(20005));
    // Nobody hinted this one: Ganesha's process is never taken for the user.
    py(&format!("os.rename('{r}/keep.bin', '{r}/kept.bin')"), 0);
    eventually("rename sent", || has(&svc, "rename 103 100 kept.bin")).await;
    assert!(actor(&svc, "rename 103 100 kept.bin").is_unknown());
    d.shutdown().await;
}

/// Codex review of #25 (P2): A's chown hint names A's chown only. B's later
/// chown of the same file, whose hint was lost, is `unknown`, never A's.
#[tokio::test(flavor = "multi_thread", worker_threads = 4)]
async fn a_later_change_never_takes_an_earlier_users_hint() {
    let Some(base) = base() else { return };
    let dir = tempfile::tempdir_in(&base).unwrap();
    let cfg = config_exe(dir.path(), "sweep_at_start = false", &python_exe());
    let (d, svc, root) = world_cfg(cfg).await;
    let f = key_of(&d, 102).await;
    let r = root.display().to_string();
    d.core
        .ns_hint_nfs(&hint(4, 2, ObjRef::default(), f, caller(20003, 1), "", ""));
    py(&format!("os.chown('{r}/a/f.bin', 4321, 4322)"), 0);
    eventually("A's chown sent", || has(&svc, "owner 102 4321 4322")).await;
    assert_eq!(actor(&svc, "owner 102 4321 4322").uid, Some(20003));
    tokio::time::sleep(Duration::from_millis(1500)).await;
    // B's chown: no hint reaches the daemon.
    py(&format!("os.chown('{r}/a/f.bin', 4331, 4332)"), 0);
    eventually("B's chown sent", || has(&svc, "owner 102 4331 4332")).await;
    let b = actor(&svc, "owner 102 4331 4332");
    assert!(b.is_unknown(), "B's chown was put on {b:?}");
    d.shutdown().await;
}

/// A local process is named by its uid (the namespace watcher's pid); the
/// daemon's own changes are never put on anyone.
#[tokio::test(flavor = "multi_thread", worker_threads = 4)]
async fn a_local_process_is_named_by_its_uid() {
    let Some(base) = base() else { return };
    let dir = tempfile::tempdir_in(&base).unwrap();
    let (d, svc, root) = world(dir.path(), "sweep_at_start = false").await;
    let r = root.display().to_string();
    py(&format!("os.rename('{r}/keep.bin', '{r}/kept.bin')"), 3);
    eventually("rename sent", || has(&svc, "rename 103 100 kept.bin")).await;
    let x = actor(&svc, "rename 103 100 kept.bin");
    assert_eq!((x.via, x.uid), (ActorVia::Local, Some(0)), "{x:?}");
    // A folder made under a temporary name and renamed into place, as smbd
    // makes them (`.::TMPNAME:D:…`): whoever moved it in made it.
    py(
        &format!(
            "os.mkdir('{r}/.::TMPNAME:D:1:made')\nos.rename('{r}/.::TMPNAME:D:1:made', '{r}/made')"
        ),
        3,
    );
    eventually("folder created", || has(&svc, "create 100 made")).await;
    let x = actor(&svc, "create 100 made");
    assert_eq!((x.via, x.uid), (ActorVia::Local, Some(0)), "{x:?}");
    d.shutdown().await;
}

/// Decision 42 (Codex review of #25, P1): a second name for a placeholder
/// whose bytes are not all local can't be copied, and must not stay as an
/// untracked alias of the managed inode: it is removed and audited. The
/// catalogued name, its marker and its bytes are untouched, and nothing is
/// tombstoned, renamed or created.
#[tokio::test(flavor = "multi_thread", worker_threads = 4)]
async fn a_hard_link_to_a_placeholder_is_removed_and_audited() {
    use std::os::unix::fs::MetadataExt;
    let Some(base) = base() else { return };
    let dir = tempfile::tempdir_in(&base).unwrap();
    let (d, svc, root) = world(dir.path(), "sweep_at_start = false").await;
    let f = root.join("a/f.bin");
    let marker = |p: &Path| xattr_of(p, "trusted.omnifile");
    let (ino, m0) = (std::fs::metadata(&f).unwrap().ino(), marker(&f));
    assert!(m0.is_some(), "a placeholder");
    // A second name in another folder, and one next to it.
    sh(&["ln", &p(&root, "a/f.bin"), &p(&root, "link.bin")]);
    sh(&["ln", &p(&root, "a/f.bin"), &p(&root, "a/twin.bin")]);
    eventually("both links removed", || {
        !root.join("link.bin").exists() && !root.join("a/twin.bin").exists()
    })
    .await;
    let st = std::fs::metadata(&f).unwrap();
    assert_eq!(
        (st.ino(), st.nlink()),
        (ino, 1),
        "the catalogued name keeps the inode"
    );
    assert_eq!(marker(&f), m0, "its marker is untouched");
    eventually("each removal audited", || {
        svc.audits()
            .iter()
            .filter(|a| a["action"] == "namespace.hardlink_removed" && a["file_id"] == "102")
            .count()
            == 2
    })
    .await;
    tokio::time::sleep(Duration::from_secs(2)).await;
    for c in ["unlink 102", "create 100 link.bin", "create 101 twin.bin"] {
        assert!(never(&svc, c), "{c}: {:?}", svc.calls());
    }
    assert!(
        svc.calls().iter().all(|c| !c.starts_with("rename 102 ")),
        "{:?}",
        svc.calls()
    );
    d.shutdown().await;
}

fn xattr_of(p: &Path, name: &str) -> Option<Vec<u8>> {
    let c = std::ffi::CString::new(p.as_os_str().as_encoded_bytes()).unwrap();
    let n = std::ffi::CString::new(name).unwrap();
    let mut buf = vec![0u8; 4096];
    // SAFETY: valid NUL-terminated strings and a buffer of its length.
    let r = unsafe { libc::lgetxattr(c.as_ptr(), n.as_ptr(), buf.as_mut_ptr().cast(), buf.len()) };
    (r >= 0).then(|| buf[..r as usize].to_vec())
}

/// Write `rel` from another process and commit it.
async fn committed_file(
    d: &Daemon<FakeService, FakeService>,
    svc: &FakeService,
    root: &Path,
    rel: &str,
    bytes: &str,
) {
    use std::os::unix::fs::MetadataExt;
    let path = root.join(rel);
    sh(&[
        "sh",
        "-c",
        &format!("printf %s '{bytes}' > '{}'", path.display()),
    ]);
    let ino = std::fs::metadata(&path).unwrap().ino();
    d.commits.send(CloseWrite::new(path, ino)).await.unwrap();
    let call = format!("create 100 {rel}");
    eventually("committed", || {
        has(svc, &call) && svc.calls().iter().any(|c| c.starts_with("upload_complete"))
    })
    .await;
}

/// Decision 42: a second name made locally or over SMB for a local file is
/// split into its own copy, committed as a separate file; never two names
/// for one catalog file, and never a rename.
#[tokio::test(flavor = "multi_thread", worker_threads = 4)]
async fn a_hard_link_to_a_local_file_is_split_and_committed_apart() {
    use std::os::unix::fs::MetadataExt;
    let Some(base) = base() else { return };
    let dir = tempfile::tempdir_in(&base).unwrap();
    let (d, svc, root) = world(dir.path(), "sweep_at_start = false").await;
    committed_file(&d, &svc, &root, "local.bin", "some local bytes").await;
    sh(&["ln", &p(&root, "local.bin"), &p(&root, "twin.bin")]);
    let split = || {
        let (a, b) = (
            std::fs::metadata(root.join("local.bin")).unwrap(),
            std::fs::metadata(root.join("twin.bin")).unwrap(),
        );
        a.ino() != b.ino() && a.nlink() == 1 && b.nlink() == 1
    };
    eventually("the second name has its own inode", split).await;
    eventually("committed as a separate file", || {
        has(&svc, "create 100 twin.bin")
    })
    .await;
    assert_eq!(
        std::fs::read(root.join("twin.bin")).unwrap(),
        b"some local bytes"
    );
    assert!(
        svc.calls()
            .iter()
            .all(|c| !(c.starts_with("rename ") && c.ends_with(" twin.bin"))),
        "{:?}",
        svc.calls()
    );
    eventually("the split is audited", || {
        svc.audits()
            .iter()
            .any(|a| a["action"] == "namespace.hardlink_split")
    })
    .await;
    d.shutdown().await;
}

/// A new file given two names before either was committed: the second is
/// split once the first is in the catalog.
#[tokio::test(flavor = "multi_thread", worker_threads = 4)]
async fn a_new_file_with_two_names_is_split_after_its_commit() {
    use std::os::unix::fs::MetadataExt;
    let Some(base) = base() else { return };
    let dir = tempfile::tempdir_in(&base).unwrap();
    let (d, svc, root) = world(dir.path(), "sweep_at_start = false").await;
    let n1 = root.join("n1.bin");
    sh(&[
        "sh",
        "-c",
        &format!(
            "echo two > '{0}' && ln '{0}' '{1}/n2.bin'",
            n1.display(),
            root.display()
        ),
    ]);
    tokio::time::sleep(Duration::from_secs(1)).await;
    let ino = std::fs::metadata(&n1).unwrap().ino();
    d.commits
        .send(CloseWrite {
            path: n1.clone(),
            ino,
            swept_gen: None,
            token: None,
            seen: None,
        })
        .await
        .unwrap();
    eventually("the first name committed", || {
        has(&svc, "create 100 n1.bin")
    })
    .await;
    let deadline = Instant::now() + Duration::from_secs(30);
    while !has(&svc, "create 100 n2.bin") {
        assert!(
            Instant::now() < deadline,
            "n2 never split: {:?}",
            svc.calls()
        );
        tokio::time::sleep(Duration::from_millis(100)).await;
    }
    let (a, b) = (
        std::fs::metadata(&n1).unwrap(),
        std::fs::metadata(root.join("n2.bin")).unwrap(),
    );
    assert!(a.ino() != b.ino() && a.nlink() == 1 && b.nlink() == 1);
    d.shutdown().await;
}

/// Line B's review case (81fa187): while the daemon is down, `mv a b;
/// mkdir a` and a file moved into `b`. The sweep at start renames (never
/// deletes) what is still on disk, renames before it creates the new `a`,
/// and says nobody (`unknown`) made the changes.
#[tokio::test(flavor = "multi_thread", worker_threads = 4)]
async fn a_rename_made_while_the_daemon_was_down_tombstones_nothing_still_on_disk() {
    let Some(base) = base() else { return };
    let dir = tempfile::tempdir_in(&base).unwrap();
    let (d, svc, root) = world(dir.path(), "sweep_at_start = false").await;
    d.shutdown().await;
    std::fs::rename(root.join("a"), root.join("b")).unwrap();
    std::fs::create_dir(root.join("a")).unwrap();
    std::fs::rename(root.join("keep.bin"), root.join("b/keep2.bin")).unwrap();
    let cfg = config(dir.path(), "sweep_at_start = true");
    let vol = startup_checks(&cfg).unwrap();
    let d = Daemon::start(cfg, vol, svc.clone(), svc.clone())
        .await
        .unwrap();
    eventually("the sweep found the moves and the new folder", || {
        has(&svc, "rename 101 100 b")
            && has(&svc, "rename 103 101 keep2.bin")
            && has(&svc, "create 100 a")
    })
    .await;
    assert!(
        pos(&svc, "rename 101 100 b") < pos(&svc, "create 100 a"),
        "the rename that frees `a` goes before the create that takes it: {:?}",
        svc.calls()
    );
    tokio::time::sleep(Duration::from_secs(3)).await;
    for id in ["101", "102", "103"] {
        assert!(
            never(&svc, &format!("unlink {id}")),
            "{id} tombstoned: {:?}",
            svc.calls()
        );
    }
    for call in [
        "rename 101 100 b",
        "rename 103 101 keep2.bin",
        "create 100 a",
    ] {
        assert!(actor(&svc, call).is_unknown(), "{call}");
    }
    assert!(root.join("b/f.bin").exists() && root.join("b/keep2.bin").exists());
    d.shutdown().await;
}
