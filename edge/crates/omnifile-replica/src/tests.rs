use serde_json::{Value, json};

use super::*;

fn share(share_id: u64, root: u64, name: &str, ev: u64) -> Value {
    json!({"type": "share.upsert", "entity_id": share_id.to_string(), "entity_version": ev.to_string(),
           "share": {"share_id": share_id.to_string(), "dataset_id": "1", "name": name,
                     "root_dir_id": root.to_string(), "protocols": ["nfs4"], "trust_level": "proven",
                     "owners_group_sid": "S-1-5-21-1-2-3-512", "on_open": "wait", "wait_threshold_minutes": null,
                     "rewind": {"depth_days": 0, "max_days": 0, "labels": {}}, "worm": {"enabled": false},
                     "entity_version": ev.to_string()}})
}

fn file(ty: &str, file_id: u64, parent: Option<u64>, name: &str, kind: &str, ev: u64) -> Value {
    json!({"type": ty, "entity_id": file_id.to_string(), "entity_version": ev.to_string(), "origin": null,
           "file": {"file_id": file_id.to_string(), "dataset_id": "1",
                    "parent_id": parent.map(|p| p.to_string()), "name": name, "kind": kind,
                    "size": 1000, "mtime_ns": "1700000000000000000", "uid": 1000, "gid": 1000, "mode": 420,
                    "state": "remote", "resident_bytes": 0, "version_id": (file_id * 10).to_string(),
                    "crc64nvme": "00000000000000ff",
                    "location": {"backend_kind": "azure_blob", "container": "a/c", "object_key": "k",
                                 "logical_tier": "hot", "archived": false, "restore_state": "not_needed"},
                    "stub": null, "entity_version": ev.to_string()}})
}

fn upsert(file_id: u64, parent: Option<u64>, name: &str, kind: &str, ev: u64) -> Value {
    file("file.upsert", file_id, parent, name, kind, ev)
}

fn tomb(file_id: u64, parent: Option<u64>, name: &str, kind: &str, ev: u64) -> Value {
    file("file.tombstone", file_id, parent, name, kind, ev)
}

fn recall(recall_id: u64, file_id: u64, version_id: u64, state: &str, ev: u64) -> Value {
    json!({"type": "recall.state", "entity_id": recall_id.to_string(), "entity_version": ev.to_string(),
           "recall": {"recall_id": recall_id.to_string(), "file_id": file_id.to_string(),
                      "version_id": version_id.to_string(), "state": state, "eta": null, "requesters": [],
                      "error": null, "read_credential": null, "entity_version": ev.to_string()}})
}

/// Run every pending op as if the filesystem did it, with an inode counter.
fn materialise_all(r: &mut Replica, ino: &mut u64) -> Vec<Action> {
    let mut done = Vec::new();
    for _ in 0..10 {
        let ops = r.pending(100).unwrap();
        if ops.is_empty() {
            break;
        }
        for op in ops {
            let out = match op.action {
                Action::Remove { .. } => Outcome::Removed,
                _ => {
                    *ino += 1;
                    let keep = op.node.ino.unwrap_or(*ino);
                    Outcome::Done {
                        ino: keep,
                        ino_gen: 1,
                        fs_version_id: op.node.content_version_id,
                        ctime_ns: None,
                    }
                }
            };
            r.record(&op, out).unwrap();
            done.push(op.action);
        }
    }
    done
}

#[test]
fn cursor_is_stored_with_the_page_and_survives_reopen() {
    let dir = tempfile::tempdir().unwrap();
    let path = dir.path().join("replica.db");
    {
        let mut r = Replica::open(&path).unwrap();
        assert_eq!(r.cursor().unwrap(), "0.0");
        r.apply_page(
            &[
                share(1, 100, "projects", 1),
                upsert(100, None, "", "dir", 1),
            ],
            "5.7",
        )
        .unwrap();
        assert_eq!(r.cursor().unwrap(), "5.7");
        let jm: String = r
            .conn()
            .pragma_query_value(None, "journal_mode", |row| row.get(0))
            .unwrap();
        assert_eq!(jm, "wal");
    }
    let r = Replica::open(&path).unwrap();
    assert_eq!(r.cursor().unwrap(), "5.7");
    // The filesystem work wasn't done before the "crash": it is redone.
    let ops = r.pending(10).unwrap();
    assert_eq!(ops.len(), 1);
    assert_eq!(
        ops[0].action,
        Action::Create {
            path: "projects".into()
        }
    );
}

#[test]
fn entity_version_guard_makes_replay_idempotent() {
    let mut r = Replica::open_in_memory().unwrap();
    let page = [
        share(1, 100, "p", 1),
        upsert(100, None, "", "dir", 1),
        upsert(101, Some(100), "a.segy", "file", 3),
    ];
    let rep = r.apply_page(&page, "1.1").unwrap();
    assert_eq!(rep.applied, 3);
    let mut ino = 0;
    materialise_all(&mut r, &mut ino);
    assert_eq!(r.pending_count().unwrap(), 0);
    let rep = r.apply_page(&page, "1.1").unwrap();
    assert_eq!(rep.applied, 0);
    assert_eq!(rep.stale, 3);
    assert_eq!(
        r.pending_count().unwrap(),
        0,
        "replay creates no filesystem work"
    );
    // An older event after a newer one is ignored.
    let rep = r
        .apply_page(&[upsert(101, Some(100), "old-name", "file", 2)], "1.2")
        .unwrap();
    assert_eq!(rep.stale, 1);
    assert_eq!(r.node_by_file_id(101).unwrap().unwrap().name, "a.segy");
}

#[test]
fn orphan_child_is_parked_until_its_parent_arrives() {
    let mut r = Replica::open_in_memory().unwrap();
    r.apply_page(
        &[share(1, 100, "p", 1), upsert(100, None, "", "dir", 1)],
        "1.0",
    )
    .unwrap();
    let mut ino = 0;
    materialise_all(&mut r, &mut ino);
    // Grandchild, then child, in the wrong order, across pages.
    let rep = r
        .apply_page(&[upsert(300, Some(200), "deep.txt", "file", 5)], "2.0")
        .unwrap();
    assert_eq!(rep.orphans_parked, 1);
    assert!(
        r.pending(10).unwrap().is_empty(),
        "an orphan is not materialised"
    );
    let rep = r
        .apply_page(&[upsert(200, Some(100), "sub", "dir", 6)], "3.0")
        .unwrap();
    assert_eq!(rep.orphans_adopted, 1);
    let done = materialise_all(&mut r, &mut ino);
    assert_eq!(
        done,
        vec![
            Action::Create {
                path: "p/sub".into()
            },
            Action::Create {
                path: "p/sub/deep.txt".into()
            }
        ]
    );
    assert_eq!(
        r.fs_path(r.node_by_file_id(300).unwrap().unwrap().lid)
            .unwrap(),
        Some("p/sub/deep.txt".into())
    );
}

#[test]
fn orphan_tombstoned_before_its_parent_is_dropped() {
    let mut r = Replica::open_in_memory().unwrap();
    r.apply_page(
        &[
            upsert(300, Some(200), "x", "file", 5),
            tomb(300, Some(200), "x", "file", 6),
        ],
        "1.0",
    )
    .unwrap();
    r.apply_page(
        &[
            share(1, 100, "p", 1),
            upsert(100, None, "", "dir", 1),
            upsert(200, Some(100), "d", "dir", 2),
        ],
        "2.0",
    )
    .unwrap();
    let mut ino = 0;
    let done = materialise_all(&mut r, &mut ino);
    assert!(
        !done
            .iter()
            .any(|a| matches!(a, Action::Create { path } if path.ends_with("x")))
    );
}

#[test]
fn tombstone_before_upsert_wins() {
    let mut r = Replica::open_in_memory().unwrap();
    r.apply_page(&[tomb(500, Some(100), "gone", "file", 9)], "1.0")
        .unwrap();
    let rep = r
        .apply_page(&[upsert(500, Some(100), "gone", "file", 8)], "2.0")
        .unwrap();
    assert_eq!(rep.stale, 1);
    assert!(r.node_by_file_id(500).unwrap().unwrap().deleted);
}

#[test]
fn directory_tombstone_before_children_retries_rmdir() {
    let mut r = Replica::open_in_memory().unwrap();
    r.apply_page(
        &[
            share(1, 100, "p", 1),
            upsert(100, None, "", "dir", 1),
            upsert(200, Some(100), "d", "dir", 2),
            upsert(201, Some(200), "f1", "file", 2),
            upsert(202, Some(200), "f2", "file", 2),
        ],
        "1.0",
    )
    .unwrap();
    let mut ino = 0;
    materialise_all(&mut r, &mut ino);
    // The directory's tombstone arrives first (another transaction).
    r.apply_page(
        &[
            tomb(200, Some(100), "d", "dir", 3),
            tomb(201, Some(200), "f1", "file", 3),
        ],
        "2.0",
    )
    .unwrap();
    let ops = r.pending(10).unwrap();
    assert_eq!(ops.len(), 2);
    assert_eq!(
        ops[0].action,
        Action::Remove {
            path: "p/d/f1".into()
        },
        "children first"
    );
    assert_eq!(ops[1].action, Action::Remove { path: "p/d".into() });
    r.record(&ops[0], Outcome::Removed).unwrap();
    // f2 is still there: ENOTEMPTY.
    r.record(&ops[1], Outcome::Retry("ENOTEMPTY".into()))
        .unwrap();
    assert_eq!(r.pending(10).unwrap().len(), 1, "rmdir stays pending");
    r.apply_page(&[tomb(202, Some(200), "f2", "file", 3)], "3.0")
        .unwrap();
    let done = materialise_all(&mut r, &mut ino);
    assert_eq!(
        done,
        vec![
            Action::Remove {
                path: "p/d/f2".into()
            },
            Action::Remove { path: "p/d".into() }
        ]
    );
    assert_eq!(r.pending_count().unwrap(), 0);
}

#[test]
fn rename_and_content_change_become_updates() {
    let mut r = Replica::open_in_memory().unwrap();
    r.apply_page(
        &[
            share(1, 100, "p", 1),
            upsert(100, None, "", "dir", 1),
            upsert(200, Some(100), "d", "dir", 1),
            upsert(201, Some(200), "f", "file", 1),
        ],
        "1.0",
    )
    .unwrap();
    let mut ino = 0;
    materialise_all(&mut r, &mut ino);
    // Directory rename: one event; children follow without their own events.
    r.apply_page(
        &[file("file.rename", 200, Some(100), "renamed", "dir", 2)],
        "2.0",
    )
    .unwrap();
    let ops = r.pending(10).unwrap();
    assert_eq!(
        ops[0].action,
        Action::Update {
            path: "p/renamed".into(),
            rename_from: Some("p/d".into()),
            content_changed: false
        }
    );
    materialise_all(&mut r, &mut ino);
    assert_eq!(
        r.fs_path(r.node_by_file_id(201).unwrap().unwrap().lid)
            .unwrap(),
        Some("p/renamed/f".into())
    );
    // Content version changes at the source.
    let mut ev = upsert(201, Some(200), "f", "file", 5);
    ev["file"]["version_id"] = json!("9999");
    r.apply_page(&[ev], "3.0").unwrap();
    let ops = r.pending(10).unwrap();
    assert_eq!(
        ops[0].action,
        Action::Update {
            path: "p/renamed/f".into(),
            rename_from: None,
            content_changed: true
        }
    );
    assert_eq!(ops[0].share_id, Some(1));
}

#[test]
fn unsafe_names_are_never_materialised() {
    let mut r = Replica::open_in_memory().unwrap();
    r.apply_page(
        &[
            share(1, 100, "p", 1),
            upsert(100, None, "", "dir", 1),
            upsert(101, Some(100), "../../etc", "file", 1),
            upsert(102, Some(100), "..", "dir", 1),
            upsert(103, Some(100), "a/b", "file", 1),
        ],
        "1.0",
    )
    .unwrap();
    let mut ino = 0;
    let done = materialise_all(&mut r, &mut ino);
    assert_eq!(done, vec![Action::Create { path: "p".into() }]);
    assert!(!valid_name("") && !valid_name(".") && valid_name("ok.segy"));
}

#[test]
fn own_echo_is_skipped_and_dirty_nodes_are_held() {
    let mut r = Replica::open_in_memory().unwrap();
    r.set_edge_id(7).unwrap();
    r.apply_page(
        &[share(1, 100, "p", 1), upsert(100, None, "", "dir", 1)],
        "1.0",
    )
    .unwrap();
    let mut ino = 0;
    materialise_all(&mut r, &mut ino);
    let root = r.node_by_file_id(100).unwrap().unwrap();
    let lid = r
        .register_local(
            Some(root.lid),
            "new.segy",
            Kind::File,
            555,
            1,
            LocalAttrs {
                size: 10,
                ..Default::default()
            },
        )
        .unwrap();
    r.outbox_enqueue("7-1", Some(lid), "create", &json!({}))
        .unwrap();
    r.outbox_set_state("7-1", "sent", None).unwrap();
    let mut echo = upsert(600, Some(100), "new.segy", "file", 2);
    echo["origin"] = json!({"edge_id": "7", "op_id": "7-1"});
    let rep = r.apply_page(&[echo], "2.0").unwrap();
    assert_eq!(rep.echoes, 1);
    assert!(r.node_by_file_id(600).unwrap().is_none());
    // The service numbers it; a foreign update arrives while local changes
    // are still unacknowledged: held back as a conflict.
    r.assign_file_id(lid, 600, 2).unwrap();
    let rep = r
        .apply_page(&[upsert(600, Some(100), "other-name", "file", 3)], "3.0")
        .unwrap();
    assert_eq!(rep.conflicts, 1);
    assert_eq!(r.node(lid).unwrap().unwrap().name, "new.segy");
    r.set_local_dirty(lid, false).unwrap();
    let rep = r
        .apply_page(&[upsert(600, Some(100), "other-name", "file", 4)], "4.0")
        .unwrap();
    assert_eq!(rep.applied, 1);
}

#[test]
fn one_active_recall_per_content_version() {
    let mut r = Replica::open_in_memory().unwrap();
    let rep = r
        .apply_page(
            &[
                recall(1, 50, 500, "requested", 1),
                recall(2, 50, 501, "rehydrating", 1),
            ],
            "1.0",
        )
        .unwrap();
    assert_eq!(rep.recalls.len(), 2);
    assert_eq!(
        r.active_recalls().unwrap().len(),
        2,
        "different versions: separate recalls"
    );
    // A newer recall id for the same key supersedes the old one.
    r.apply_page(&[recall(3, 50, 500, "requested", 1)], "2.0")
        .unwrap();
    assert_eq!(r.active_recall_for(50, 500).unwrap().unwrap().recall_id, 3);
    assert_eq!(r.recall(1).unwrap().unwrap().state, "cancelled");
    // Stale state is ignored; terminal states leave the active set.
    let rep = r
        .apply_page(&[recall(3, 50, 500, "downloading", 1)], "3.0")
        .unwrap();
    assert_eq!(rep.stale, 1);
    r.apply_page(&[recall(3, 50, 500, "available", 2)], "4.0")
        .unwrap();
    assert!(r.active_recall_for(50, 500).unwrap().is_none());
}

#[test]
fn stub_wanted_and_status() {
    let mut r = Replica::open_in_memory().unwrap();
    let mut f = upsert(101, Some(100), "big.segy", "file", 2);
    f["file"]["stub"] = json!({"version_id": "1010", "resident_bytes": 4096});
    r.apply_page(
        &[
            share(1, 100, "p", 1),
            upsert(100, None, "", "dir", 1),
            f.clone(),
        ],
        "1.0",
    )
    .unwrap();
    let mut ino = 0;
    materialise_all(&mut r, &mut ino);
    // No recorded ctime: the stubber cannot prove the bytes are the
    // committed version, so the file is not offered (N6).
    assert!(r.stub_wanted(10).unwrap().is_empty());
    let lid = r.node_by_file_id(101).unwrap().unwrap().lid;
    r.set_fs_ctime(lid, 42).unwrap();
    // Local changes the service hasn't acknowledged: not offered either.
    r.set_local_dirty(lid, true).unwrap();
    assert!(r.stub_wanted(10).unwrap().is_empty());
    r.set_local_dirty(lid, false).unwrap();
    let w = r.stub_wanted(10).unwrap();
    assert_eq!(w.len(), 1);
    assert_eq!((w[0].version_id, w[0].resident_bytes), (1010, 4096));
    assert_eq!(w[0].path, PathBuf::from("p/big.segy"));
    r.set_stub_status(w[0].node.lid, stub_status::DONE).unwrap();
    // A later unrelated update with the same stub request keeps "done".
    f["file"]["mode"] = json!(384);
    f["entity_version"] = json!("3");
    r.apply_page(&[f], "2.0").unwrap();
    materialise_all(&mut r, &mut ino);
    assert!(r.stub_wanted(10).unwrap().is_empty());
    // A cleared ctime (a write after the commit's marker) also hides it.
    r.set_stub_status(lid, stub_status::WANTED).unwrap();
    assert_eq!(r.stub_wanted(10).unwrap().len(), 1);
    r.clear_fs_ctime(lid).unwrap();
    assert!(r.stub_wanted(10).unwrap().is_empty());
}

#[test]
fn malformed_and_unknown_events_still_advance_the_cursor() {
    let mut r = Replica::open_in_memory().unwrap();
    let rep = r
        .apply_page(
            &[
                json!({"type": "file.upsert"}),
                json!({"type": "future.thing", "entity_id": "1"}),
            ],
            "9.9",
        )
        .unwrap();
    assert_eq!((rep.malformed, rep.unknown), (1, 1));
    assert_eq!(r.cursor().unwrap(), "9.9");
}

#[test]
fn counters_are_persistent_and_monotonic() {
    let mut r = Replica::open_in_memory().unwrap();
    assert_eq!(r.next_counter("op").unwrap(), 1);
    assert_eq!(r.next_counter("op").unwrap(), 2);
    assert_eq!(r.reserve_counter("op", 10).unwrap(), 3);
    assert_eq!(r.next_counter("op").unwrap(), 13);
}

mod props {
    use super::*;
    use proptest::prelude::*;

    proptest! {
        #![proptest_config(ProptestConfig { cases: 64, ..ProptestConfig::default() })]
        /// Any order of a fixed set of events converges to the same tree once
        /// everything is applied and materialised.
        #[test]
        fn order_independent_convergence(perm in Just((0..6usize).collect::<Vec<_>>()).prop_shuffle()) {
            let events = [
                share(1, 100, "p", 1),
                upsert(100, None, "", "dir", 1),
                upsert(200, Some(100), "a", "dir", 1),
                upsert(201, Some(200), "b", "dir", 1),
                upsert(301, Some(201), "c.segy", "file", 1),
                upsert(302, Some(200), "d.segy", "file", 1),
            ];
            let mut r = Replica::open_in_memory().unwrap();
            let mut ino = 0;
            for (i, idx) in perm.iter().enumerate() {
                r.apply_page(&[events[*idx].clone()], &format!("{i}.0")).unwrap();
                materialise_all(&mut r, &mut ino);
            }
            prop_assert_eq!(r.pending_count().unwrap(), 0);
            let path = |fid| r.fs_path(r.node_by_file_id(fid).unwrap().unwrap().lid).unwrap();
            prop_assert_eq!(path(301), Some(PathBuf::from("p/a/b/c.segy")));
            prop_assert_eq!(path(302), Some(PathBuf::from("p/a/d.segy")));
        }
    }
}

#[test]
fn an_acl_change_is_reported_for_the_rewind_trees() {
    let mut r = Replica::open_in_memory().unwrap();
    let rep = r
        .apply_page(
            &[share(1, 100, "p", 1), upsert(100, None, "", "dir", 1)],
            "1.1",
        )
        .unwrap();
    assert!(!rep.acls_changed);
    let acl = |ev: u64, body: &str| {
        json!({"type": "acl.upsert", "entity_id": "7", "entity_version": ev.to_string(),
               "acl": {"acl_id": "7", "dataset_id": "1", "sha256": body, "mode": 504,
                       "entries": [], "entity_version": ev.to_string()}})
    };
    assert!(r.apply_page(&[acl(1, "a")], "1.2").unwrap().acls_changed);
    // The same version again: nothing changed.
    assert!(!r.apply_page(&[acl(1, "a")], "1.3").unwrap().acls_changed);
    // A directory now has another ACL.
    let mut dir = upsert(100, None, "", "dir", 2);
    dir["file"]["acl_id"] = json!("7");
    assert!(r.apply_page(&[dir], "1.4").unwrap().acls_changed);
    // A file's content changing is not an ACL change.
    let rep = r
        .apply_page(&[upsert(101, Some(100), "a.segy", "file", 3)], "1.5")
        .unwrap();
    assert!(!rep.acls_changed);
}

#[test]
fn a_resnapshot_after_a_restore_applies_wound_back_rows_and_finds_lost_ones() {
    let mut r = Replica::open_in_memory().unwrap();
    let page = [
        share(1, 100, "proj", 3),
        upsert(100, None, "", "dir", 1),
        upsert(101, Some(100), "a.segy", "file", 5),
        upsert(102, Some(100), "b.segy", "file", 5),
        // Created after the restore point: the restored catalog never had it.
        upsert(103, Some(100), "late.segy", "file", 2),
    ];
    r.apply_page(&page, "9.0").unwrap();
    let mut ino = 0;
    materialise_all(&mut r, &mut ino);

    r.resnapshot_begin().unwrap();
    // The restored share is named differently, at a lower entity_version.
    let mut sh = share(1, 100, "proj-restored", 1)["share"].clone();
    sh["entity_version"] = json!("1");
    r.resnapshot_meta(None, &[sh], &[]).unwrap();
    assert_eq!(r.shares().unwrap()[0].name, "proj-restored");
    // a.segy was renamed after the restore point: the catalog has its old
    // name, at a lower entity_version, and that wins.
    let root = upsert(100, None, "", "dir", 1)["file"].clone();
    let a = upsert(101, Some(100), "a-old.segy", "file", 2)["file"].clone();
    // b.segy: the edge holds a newer content version than the catalog.
    let mut b = upsert(102, Some(100), "b.segy", "file", 2)["file"].clone();
    b["version_id"] = json!("1000");
    r.resnapshot_files(&[root, a, b]).unwrap();
    assert_eq!(r.node_by_file_id(101).unwrap().unwrap().name, "a-old.segy");

    let lost = r.resnapshot_lost(1).unwrap();
    assert_eq!(lost.len(), 1);
    assert_eq!(lost[0].0.file_id, Some(103));
    assert_eq!(
        lost[0].1.as_deref(),
        Some(std::path::Path::new("proj/late.segy"))
    );
    assert!(r.node_by_file_id(103).unwrap().is_none(), "its row is gone");

    let newer = r.resnapshot_newer().unwrap();
    assert_eq!(newer.len(), 1);
    assert_eq!(newer[0].0.file_id, Some(102));
    assert_eq!(newer[0].0.content_version_id, Some(1020), "not applied yet");
    // A stub: the held row is applied, and shows the catalog's version.
    r.resnapshot_apply_held(&newer[0].2).unwrap();
    assert_eq!(
        r.node_by_file_id(102).unwrap().unwrap().content_version_id,
        Some(1000)
    );

    r.resnapshot_finish("12.0.1").unwrap();
    assert_eq!(r.cursor().unwrap(), "12.0.1");
    // Later feed events, at versions above the restored ones, apply.
    let rep = r
        .apply_page(&[upsert(101, Some(100), "a-new.segy", "file", 3)], "13.0.1")
        .unwrap();
    assert_eq!(rep.applied, 1);
}

#[test]
fn audit_records_wait_for_the_service_and_a_full_buffer_says_what_it_dropped() {
    let mut r = Replica::open_in_memory().unwrap();
    let rec = |i: i64| serde_json::json!({"action": "read.refused", "n": i});
    r.audit_push(&[rec(1), rec(2), rec(3)], "2026-10-15T10:00:00Z")
        .unwrap();
    let got = r.audit_peek(2).unwrap();
    assert_eq!(got.len(), 2);
    assert_eq!(got[0].1["n"], 1);
    r.audit_ack(got[1].0).unwrap();
    let rest = r.audit_peek(10).unwrap();
    assert_eq!(rest.len(), 1);
    assert_eq!(rest[0].1["n"], 3);

    // Past the cap the oldest go, and one note says how many.
    let many: Vec<_> = (0..Replica::AUDIT_MAX).map(rec).collect();
    r.audit_push(&many, "2026-10-15T11:00:00Z").unwrap();
    let all = r.audit_peek(usize::MAX >> 1).unwrap();
    assert_eq!(all.len() as i64, Replica::AUDIT_MAX);
    let note = &all.last().unwrap().1;
    assert_eq!(note["action"], "audit.dropped");
    assert_eq!(note["details"]["dropped"], 2);
    assert_eq!(note["at"], "2026-10-15T11:00:00Z");
    // Dropped: the one left from before, and the batch's first.
    assert_eq!(all[0].1["n"], 1);
}

#[test]
fn the_digest_matches_the_services_definition() {
    // SHA-256("1:1"), "2:1", "3:1": the same value Postgres and Python give.
    let x = digest_row(1, 1) ^ digest_row(2, 1) ^ digest_row(3, 1);
    assert_eq!(format!("{x:016x}"), "ffb34c1412bd6b86");
}

/// M6, shares of folders and removal: a file belongs to the nearest share
/// above it; a share.delete drops the share (only a newer one), and the
/// tree stays.
#[test]
fn the_nearest_share_holds_a_file_and_a_removed_share_goes() {
    let dir = tempfile::tempdir().unwrap();
    let mut r = Replica::open(&dir.path().join("r.db")).unwrap();
    r.apply_page(
        &[
            share(1, 100, "proj", 1),
            share(2, 200, "seis", 1),
            upsert(100, None, "", "dir", 1),
            upsert(200, Some(100), "seismic", "dir", 1),
            upsert(300, Some(200), "a.segy", "file", 1),
            upsert(400, Some(100), "b.segy", "file", 1),
        ],
        "1.0",
    )
    .unwrap();
    let mut ino = 0;
    materialise_all(&mut r, &mut ino);
    let lid = |r: &Replica, f| r.node_by_file_id(f).unwrap().unwrap().lid;
    let of = |r: &Replica, f| r.share_of(lid(r, f)).unwrap().map(|s| s.share_id);
    assert_eq!(of(&r, 300), Some(2), "under the shared folder: its share");
    assert_eq!(of(&r, 200), Some(2), "the folder itself");
    assert_eq!(of(&r, 400), Some(1), "elsewhere: the root's share");

    // An old delete (version not newer) changes nothing.
    let del = |ev: u64| json!({"type": "share.delete", "entity_id": "2", "entity_version": ev.to_string(), "share_id": "2"});
    r.apply_page(&[del(1)], "2.0").unwrap();
    assert!(r.share(2).unwrap().is_some());
    r.apply_page(&[del(2)], "3.0").unwrap();
    assert!(r.share(2).unwrap().is_none());
    assert_eq!(of(&r, 300), Some(1), "back to the root's share");
    assert!(
        r.node_by_file_id(200).unwrap().is_some(),
        "the folder stays"
    );
}

#[test]
fn a_file_never_gets_set_id_bits_from_the_catalog() {
    // M7 review: a row with uid 0 and 04755 would put a root executable on
    // every NFS client that mounts without nosuid.
    let mut f = upsert(9, Some(1), "tool", "file", 1);
    f["file"]["uid"] = 0.into();
    f["file"]["mode"] = 0o6755.into();
    let row = feed::parse_file_row(&f["file"]).unwrap();
    assert_eq!(row.mode, 0o755);
    // A directory keeps setgid (group inheritance) and the sticky bit.
    let mut d = upsert(10, Some(1), "shared", "dir", 1);
    d["file"]["mode"] = 0o7775.into();
    assert_eq!(feed::parse_file_row(&d["file"]).unwrap().mode, 0o3775);
}

#[test]
fn a_local_rename_is_kept_and_its_feed_changes_wait_for_the_ack() {
    let mut r = Replica::open_in_memory().unwrap();
    r.set_edge_id(7).unwrap();
    r.apply_page(
        &[
            share(1, 100, "p", 1),
            upsert(100, None, "", "dir", 1),
            upsert(101, Some(100), "a", "dir", 1),
            upsert(102, Some(101), "f.bin", "file", 1),
        ],
        "1.0",
    )
    .unwrap();
    let mut ino = 0;
    materialise_all(&mut r, &mut ino);
    let (root, f) = (
        r.node_by_file_id(100).unwrap().unwrap(),
        r.node_by_file_id(102).unwrap().unwrap(),
    );
    // f.bin moved by a user to the root as g.bin.
    r.local_moved(f.lid, root.lid, "g.bin", "7-9", None)
        .unwrap();
    assert!(
        materialise_all(&mut r, &mut ino).is_empty(),
        "the materialiser has nothing to move back"
    );
    assert_eq!(
        r.fs_path(f.lid).unwrap().unwrap(),
        std::path::PathBuf::from("p/g.bin")
    );
    // A catalog change to it while the rename is unacknowledged: held.
    let rep = r
        .apply_page(&[upsert(102, Some(101), "other.bin", "file", 2)], "2.0")
        .unwrap();
    assert_eq!(rep.conflicts, 1);
    assert_eq!(r.node(f.lid).unwrap().unwrap().name, "g.bin");
    // Sent and acknowledged: the echo is the edge's own, and skipped.
    r.outbox_set_state("7-9", "sent", None).unwrap();
    // A held event means the row is fetched again (M7 review S3), and the
    // node's version stays where it was so that row isn't taken for stale.
    assert!(
        r.ns_applied("7-9", f.lid, Some(3)).unwrap(),
        "the hold is reported"
    );
    assert_eq!(r.node(f.lid).unwrap().unwrap().entity_version, 1);
    let mut echo = upsert(102, Some(100), "g.bin", "file", 3);
    echo["origin"] = json!({"edge_id": "7", "op_id": "7-9"});
    assert_eq!(r.apply_page(&[echo.clone()], "3.0").unwrap().echoes, 1);
    // The row fetched again (version 3: the rename plus what was held) applies.
    let mut fetched = upsert(102, Some(100), "g.bin", "file", 3)["file"].clone();
    fetched["size"] = json!(77);
    assert_eq!(r.apply_snapshot_rows(&[fetched]).unwrap().applied, 1);
    assert_eq!(r.node(f.lid).unwrap().unwrap().entity_version, 3);
    // Later catalog changes apply again.
    let rep = r
        .apply_page(&[upsert(102, Some(100), "h.bin", "file", 4)], "4.0")
        .unwrap();
    assert_eq!(rep.applied, 1);
    assert!(r.ns_unacked(10, 60).unwrap().is_empty());
}

#[test]
fn a_local_delete_takes_the_subtree_deepest_first_and_nothing_comes_back() {
    let mut r = Replica::open_in_memory().unwrap();
    r.apply_page(
        &[
            share(1, 100, "p", 1),
            upsert(100, None, "", "dir", 1),
            upsert(101, Some(100), "a", "dir", 1),
            upsert(102, Some(101), "b", "dir", 1),
            upsert(103, Some(102), "f.bin", "file", 1),
            upsert(104, Some(101), "g.bin", "file", 1),
        ],
        "1.0",
    )
    .unwrap();
    let mut ino = 0;
    materialise_all(&mut r, &mut ino);
    let a = r.node_by_file_id(101).unwrap().unwrap();
    let order: Vec<u64> = r
        .subtree_postorder(a.lid)
        .unwrap()
        .iter()
        .map(|n| n.file_id.unwrap())
        .collect();
    assert_eq!(order.last(), Some(&101), "the folder last");
    let pos = |id| order.iter().position(|x| *x == id).unwrap();
    assert!(pos(103) < pos(102), "a child before its folder");
    // Who deleted it rides with each op (control-api.md 4.1 `actor`).
    let who = serde_json::json!({"via": "smb", "uid": 20002});
    let ops: Vec<(i64, String, Option<serde_json::Value>)> = r
        .subtree_postorder(a.lid)
        .unwrap()
        .iter()
        .enumerate()
        .map(|(i, n)| (n.lid, format!("7-{i}"), (i == 0).then(|| who.clone())))
        .collect();
    r.local_deleted(&ops).unwrap();
    assert!(
        materialise_all(&mut r, &mut ino).is_empty(),
        "nothing is put back"
    );
    let queued: Vec<String> = r
        .ns_unacked(10, 60)
        .unwrap()
        .iter()
        .map(|o| format!("{}:{}", o.op, o.lid.unwrap()))
        .collect();
    assert_eq!(queued.len(), 4);
    assert!(queued.iter().all(|q| q.starts_with("unlink:")));
    assert!(r.node_by_file_id(101).unwrap().unwrap().deleted);
    let payloads: Vec<serde_json::Value> = r
        .ns_unacked(10, 60)
        .unwrap()
        .into_iter()
        .map(|o| o.payload)
        .collect();
    assert_eq!(payloads[0]["actor"], who);
    assert!(payloads[1..].iter().all(|p| p.get("actor").is_none()));
}
