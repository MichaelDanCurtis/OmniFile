use std::sync::Mutex;
use std::sync::atomic::{AtomicUsize, Ordering};
use std::time::Duration;

use crate::server::*;
use crate::testing::{FakeFsal, MemIo, pair};
use crate::wire::*;

#[derive(Default)]
struct TestHandler {
    decision: Mutex<Option<ReadDecision>>,
    slow: bool,
    notes: AtomicUsize,
    states: Vec<RecallState>,
}

impl Handler for TestHandler {
    async fn read_intent(&self, _conn: ConnInfo, req: ReadIntent) -> ReadDecision {
        if self.slow {
            tokio::time::sleep(Duration::from_secs(60)).await;
        }
        self.decision.lock().unwrap().unwrap_or(ReadDecision {
            decision: Decision::Delay,
            reason: reason::RECALL_STARTED,
            eta_s: 60,
            ttl_ms: 0,
            recall_id: req.version_id + 1000,
            resident_bytes: 0,
        })
    }
    async fn populate_dir(&self, _conn: ConnInfo, _req: PopulateDir) -> PopulateResult {
        PopulateResult {
            result: 2,
            eta_ms: 100,
        }
    }
    fn notification(&self, _conn: ConnInfo, _n: Notification) {
        self.notes.fetch_add(1, Ordering::SeqCst);
    }
    fn resync_states(&self) -> Vec<RecallState> {
        self.states.clone()
    }
}

fn cfg() -> ServerConfig {
    ServerConfig {
        handler_timeout: Duration::from_millis(200),
        invalidate_timeout: Duration::from_millis(300),
        required_exe: Some("/usr/bin/ganesha.nfsd".into()),
        ..Default::default()
    }
}

fn start(h: TestHandler, c: ServerConfig) -> (Server<TestHandler>, FakeFsal<MemIo>) {
    let server = Server::new(c, h);
    let (a, b) = pair();
    let s = server.clone();
    tokio::spawn(async move {
        s.serve(a, 100, 0, Some("/usr/bin/ganesha.nfsd".into()))
            .await
    });
    (server, FakeFsal::new(b))
}

fn obj() -> ObjRef {
    ObjRef {
        fsid: 1,
        ino: 2,
        ino_gen: 3,
        export_id: 4,
        file_id: 5,
    }
}

fn intent(policy: Policy, flags: u16) -> ReadIntent {
    ReadIntent {
        obj: obj(),
        version_id: 7,
        offset: 0,
        length: 4096,
        size: 1 << 20,
        resident_bytes: 0,
        op: ReadOp::Read,
        policy,
        marker_flags: flags,
        quick_ms: 0,
        parked: false,
        identity: Identity::default(),
        principal: vec![],
        path_hint: b"/share/f".to_vec(),
    }
}

fn recall_state(id: u64) -> RecallState {
    RecallState {
        obj: obj(),
        version_id: 7,
        recall_id: id,
        state: RecallPhase::Restoring,
        answer: Decision::Delay,
        eta_s: 100,
        resident_bytes: 0,
        valid_ms: 10_000,
    }
}

#[tokio::test]
async fn handshake_negotiates_and_resyncs() {
    let h = TestHandler {
        states: vec![recall_state(1), recall_state(2)],
        ..Default::default()
    };
    let (server, mut f) = start(h, cfg());
    let (ack, states) = f.handshake().await;
    assert_eq!(ack.proto_major, 0);
    assert_eq!(ack.proto_minor, 1);
    assert_eq!(ack.instance, server.config().instance);
    assert_eq!(ack.features & feature::PARK, feature::PARK);
    assert_eq!(states.len(), 2);
    assert_eq!(server.ready_connections(), 1);
}

/// M5 (v0.2): `CHUNK_MAP` only for a peer that negotiates minor 2; a v0.1
/// FSAL that sets the bit anyway gets prefix-only service, and keeps
/// `POPULATE_DIR` (a v0.1 feature on this line).
#[tokio::test]
async fn chunk_map_needs_minor_2_and_old_peers_keep_their_features() {
    let all = feature::PARK | feature::CHUNK_MAP | feature::POPULATE_DIR;
    let (server, mut old) = start(TestHandler::default(), cfg());
    let (ack, _) = old.handshake_with(1, all).await;
    assert_eq!(ack.proto_minor, 1);
    assert_eq!(ack.features & feature::CHUNK_MAP, 0, "v0.1: no chunk maps");
    assert_eq!(ack.features & feature::POPULATE_DIR, feature::POPULATE_DIR);
    assert_eq!(
        server.common_features().map(|f| f & feature::CHUNK_MAP),
        Some(0)
    );
    drop(old);

    let (server, mut new) = start(TestHandler::default(), cfg());
    assert_eq!(server.common_features(), None, "no ready connection");
    let (ack, _) = new.handshake_with(2, all).await;
    assert_eq!(ack.proto_minor, 2);
    assert_eq!(ack.features, all);
    assert_eq!(server.common_features(), Some(all));
    // A newer FSAL negotiates down to this daemon's minor.
    let (server, mut newer) = start(TestHandler::default(), cfg());
    let (ack, _) = newer.handshake_with(PROTO_MINOR + 1, all).await;
    assert_eq!(ack.proto_minor, PROTO_MINOR);
    assert_eq!(ack.features & feature::CHUNK_MAP, feature::CHUNK_MAP);
    drop(server);
}

#[tokio::test]
async fn read_intent_gets_handler_decision_with_req_id() {
    let (_server, mut f) = start(TestHandler::default(), cfg());
    f.handshake().await;
    let id = f.send(&Message::ReadIntent(intent(Policy::Wait, 0))).await;
    let fr = f.recv().await;
    assert_eq!(fr.header.req_id, id);
    let Message::ReadDecision(d) = fr.msg else {
        panic!()
    };
    assert_eq!(d.decision, Decision::Delay);
    assert_eq!(d.recall_id, 1007);
}

#[tokio::test]
async fn slow_handler_fails_closed_and_ping_still_answered() {
    let h = TestHandler {
        slow: true,
        ..Default::default()
    };
    let (_server, mut f) = start(h, cfg());
    f.handshake().await;
    let rid = f
        .send(&Message::ReadIntent(intent(Policy::FailFast, 0)))
        .await;
    // PING is answered while the handler is stuck.
    let pid = f.send(&Message::Ping { mono_ns: 77 }).await;
    let fr = f.recv().await;
    assert_eq!(fr.header.req_id, pid);
    assert_eq!(fr.msg, Message::Pong { mono_ns: 77 });
    let fr = f.recv().await;
    assert_eq!(fr.header.req_id, rid);
    let Message::ReadDecision(d) = fr.msg else {
        panic!()
    };
    assert_eq!(d.decision, Decision::Eio, "failfast fail-closed answer");
}

#[tokio::test]
async fn park_downgraded_unless_negotiated_and_online() {
    let h = TestHandler::default();
    *h.decision.lock().unwrap() = Some(ReadDecision::simple(
        Decision::Park,
        reason::FETCH_IN_PROGRESS,
    ));
    let (_server, mut f) = start(h, cfg());
    f.handshake().await;
    // Not ONLINE: DELAY for wait.
    f.send(&Message::ReadIntent(intent(Policy::Wait, 0))).await;
    let Message::ReadDecision(d) = f.recv().await.msg else {
        panic!()
    };
    assert_eq!(d.decision, Decision::Delay);
    // ONLINE: PARK passes.
    f.send(&Message::ReadIntent(intent(Policy::Wait, 1))).await;
    let Message::ReadDecision(d) = f.recv().await.msg else {
        panic!()
    };
    assert_eq!(d.decision, Decision::Park);
    // ONLINE but a write needing a full fill: never parked.
    let mut w = intent(Policy::FailFast, 1);
    w.op = ReadOp::Write;
    f.send(&Message::ReadIntent(w)).await;
    let Message::ReadDecision(d) = f.recv().await.msg else {
        panic!()
    };
    assert_eq!(d.decision, Decision::Eio);
}

#[test]
fn unknown_decision_code_becomes_policy_default() {
    let d = sanitize_decision(
        ReadDecision::simple(Decision::Other(9), 0),
        &intent(Policy::Wait, 1),
        0,
    );
    assert_eq!(d.decision, Decision::Delay);
    let d = sanitize_decision(
        ReadDecision::simple(Decision::Other(9), 0),
        &intent(Policy::Other(7), 1),
        0,
    );
    assert_eq!(d.decision, Decision::Eio);
}

#[tokio::test]
async fn invalidate_waits_for_every_ack() {
    let (server, mut f) = start(TestHandler::default(), cfg());
    f.handshake().await;
    let s = server.clone();
    let task = tokio::spawn(async move { s.invalidate(obj(), what::MARKER, true).await });
    let fr = f.recv().await;
    assert!(fr.header.req_id & DAEMON_REQ_BIT != 0);
    let Message::Invalidate(inv) = fr.msg else {
        panic!()
    };
    assert_eq!(inv.flags & INVALIDATE_ACK_REQUIRED, INVALIDATE_ACK_REQUIRED);
    f.send_with_id(
        &Message::InvalidateAck {
            inval_seq: inv.inval_seq,
        },
        fr.header.req_id,
    )
    .await;
    let out = task.await.unwrap();
    assert!(out.all_acked());
    assert_eq!(out.connections, 1);
}

#[tokio::test]
async fn invalidate_without_ack_is_not_all_acked() {
    let (server, mut f) = start(TestHandler::default(), cfg());
    f.handshake().await;
    let out = server.invalidate(obj(), what::MARKER, true).await;
    assert!(!out.all_acked());
    // A wrong inval_seq is not an ack either.
    let s = server.clone();
    let task = tokio::spawn(async move { s.invalidate(obj(), what::MARKER, true).await });
    let _stale = f.recv().await;
    let fr = f.recv().await;
    let Message::Invalidate(_) = fr.msg else {
        panic!()
    };
    f.send_with_id(
        &Message::InvalidateAck { inval_seq: 999_999 },
        fr.header.req_id,
    )
    .await;
    assert!(!task.await.unwrap().all_acked());
}

#[tokio::test]
async fn invalidate_with_no_fsal_is_not_acked() {
    let server = Server::new(cfg(), TestHandler::default());
    let out = server.invalidate(obj(), what::MARKER, true).await;
    assert_eq!(out.connections, 0);
    assert!(!out.all_acked(), "silence is not an acknowledgement");
    assert!(out.no_fsal());
}

#[tokio::test]
async fn a_connection_still_in_hello_is_not_acked() {
    // One ready FSAL that acks, plus one accepted connection that has not
    // sent HELLO yet: not every FSAL acknowledged.
    let (server, mut f) = start(TestHandler::default(), cfg());
    f.handshake().await;
    let (a, _b) = pair();
    let s = server.clone();
    tokio::spawn(async move {
        s.serve(a, 100, 0, Some("/usr/bin/ganesha.nfsd".into()))
            .await
    });
    tokio::time::sleep(Duration::from_millis(50)).await;
    let s = server.clone();
    let task = tokio::spawn(async move { s.invalidate(obj(), what::MARKER, true).await });
    let fr = f.recv().await;
    let Message::Invalidate(inv) = fr.msg else {
        panic!()
    };
    f.send_with_id(
        &Message::InvalidateAck {
            inval_seq: inv.inval_seq,
        },
        fr.header.req_id,
    )
    .await;
    let out = task.await.unwrap();
    assert_eq!((out.connections, out.acked, out.not_ready), (1, 1, 1));
    assert!(!out.all_acked());
    assert!(!out.no_fsal());
}

#[tokio::test]
async fn a_timed_out_ack_waiter_is_forgotten() {
    let (server, mut f) = start(TestHandler::default(), cfg());
    f.handshake().await;
    let out = server.invalidate(obj(), what::MARKER, true).await;
    assert!(!out.all_acked());
    assert_eq!(
        server.pending_acks(),
        0,
        "timed-out waiter left in the table"
    );
    // A late ack is harmless.
    let fr = f.recv().await;
    let Message::Invalidate(inv) = fr.msg else {
        panic!()
    };
    f.send_with_id(
        &Message::InvalidateAck {
            inval_seq: inv.inval_seq,
        },
        fr.header.req_id,
    )
    .await;
    tokio::time::sleep(Duration::from_millis(20)).await;
    assert_eq!(server.ready_connections(), 1);
}

#[tokio::test]
async fn messages_before_hello_are_malformed() {
    let (_server, mut f) = start(TestHandler::default(), cfg());
    f.send(&Message::ReadIntent(intent(Policy::Wait, 0))).await;
    let Message::Error { code, .. } = f.recv().await.msg else {
        panic!()
    };
    assert_eq!(code, error_code::MALFORMED);
    assert!(
        f.recv_timeout(Duration::from_millis(500)).await.is_none(),
        "connection closed"
    );
}

#[tokio::test]
async fn version_mismatch_closes() {
    let (_server, mut f) = start(TestHandler::default(), cfg());
    f.send(&Message::Hello(Hello {
        proto_major: 1,
        proto_minor: 0,
        features: 0,
        pid: 1,
        instance: 1,
        ganesha_version: vec![],
        fsal_build: vec![],
    }))
    .await;
    let Message::Error { code, .. } = f.recv().await.msg else {
        panic!()
    };
    assert_eq!(code, error_code::VERSION_MISMATCH);
}

#[tokio::test]
async fn unknown_request_gets_error_2_and_stays_up() {
    let (_server, mut f) = start(TestHandler::default(), cfg());
    f.handshake().await;
    let id = f
        .send(&Message::Unknown {
            ty: 0x0123,
            body: vec![1, 2, 3],
        })
        .await;
    let fr = f.recv().await;
    assert_eq!(fr.header.req_id, id);
    assert!(matches!(
        fr.msg,
        Message::Error {
            code: error_code::UNKNOWN_REQUEST,
            ..
        }
    ));
    // Unknown notification (req_id 0): ignored.
    f.send_with_id(
        &Message::Unknown {
            ty: 0x0124,
            body: vec![],
        },
        0,
    )
    .await;
    f.send(&Message::Ping { mono_ns: 1 }).await;
    assert_eq!(f.recv().await.msg, Message::Pong { mono_ns: 1 });
}

#[tokio::test]
async fn short_fixed_part_is_malformed() {
    let (_server, mut f) = start(TestHandler::default(), cfg());
    f.handshake().await;
    let mut rec = encode(&Message::Ping { mono_ns: 1 }, 5);
    rec.truncate(20);
    rec[4..8].copy_from_slice(&20u32.to_le_bytes());
    f.send_raw(&rec).await;
    let Message::Error { code, .. } = f.recv().await.msg else {
        panic!()
    };
    assert_eq!(code, error_code::MALFORMED);
}

#[tokio::test]
async fn wrong_peer_refused() {
    let server = Server::new(cfg(), TestHandler::default());
    let (a, b) = pair();
    let s = server.clone();
    tokio::spawn(async move {
        s.serve(a, 100, 1000, Some("/usr/bin/ganesha.nfsd".into()))
            .await
    });
    let mut f = FakeFsal::new(b);
    let Message::Error { code, .. } = f.recv().await.msg else {
        panic!()
    };
    assert_eq!(code, error_code::PEER_NOT_ALLOWED);

    let (a, b) = pair();
    let s = server.clone();
    tokio::spawn(async move { s.serve(a, 100, 0, Some("/usr/bin/python3".into())).await });
    let mut f = FakeFsal::new(b);
    let Message::Error { code, .. } = f.recv().await.msg else {
        panic!()
    };
    assert_eq!(code, error_code::PEER_NOT_ALLOWED);

    // Same file name, another directory: refused (the full path is pinned).
    let (a, b) = pair();
    let s = server.clone();
    tokio::spawn(async move {
        s.serve(a, 100, 0, Some("/tmp/evil/ganesha.nfsd".into()))
            .await
    });
    let mut f = FakeFsal::new(b);
    let Message::Error { code, .. } = f.recv().await.msg else {
        panic!()
    };
    assert_eq!(code, error_code::PEER_NOT_ALLOWED);

    // A configured bare name matches nothing.
    let bare = Server::new(
        ServerConfig {
            required_exe: Some("ganesha.nfsd".into()),
            ..cfg()
        },
        TestHandler::default(),
    );
    let (a, b) = pair();
    tokio::spawn(async move {
        bare.serve(a, 100, 0, Some("/usr/bin/ganesha.nfsd".into()))
            .await
    });
    let mut f = FakeFsal::new(b);
    let Message::Error { code, .. } = f.recv().await.msg else {
        panic!()
    };
    assert_eq!(code, error_code::PEER_NOT_ALLOWED);
}

#[tokio::test]
async fn push_and_notifications() {
    let (server, mut f) = start(TestHandler::default(), cfg());
    f.handshake().await;
    server.push_recall_state(recall_state(5));
    let Message::RecallState(s) = f.recv().await.msg else {
        panic!()
    };
    assert_eq!(s.recall_id, 5);
    f.send_with_id(
        &Message::BadMarker(BadMarker {
            obj: obj(),
            reason: 2,
            errno: 0,
        }),
        0,
    )
    .await;
    f.send(&Message::Ping { mono_ns: 3 }).await;
    f.recv().await;
    assert_eq!(server.handler().notes.load(Ordering::SeqCst), 1);
}

#[tokio::test]
async fn shutdown_sends_error_5_and_closes() {
    let (server, mut f) = start(TestHandler::default(), cfg());
    f.handshake().await;
    server.shutdown().await;
    let Message::Error { code, .. } = f.recv().await.msg else {
        panic!()
    };
    assert_eq!(code, error_code::SHUTTING_DOWN);
    assert!(f.recv_timeout(Duration::from_millis(300)).await.is_none());
    assert_eq!(server.ready_connections(), 0);
}

#[tokio::test]
async fn idle_connection_is_closed() {
    let c = ServerConfig {
        idle_timeout: Duration::from_millis(200),
        ..cfg()
    };
    let (server, mut f) = start(TestHandler::default(), c);
    f.handshake().await;
    tokio::time::sleep(Duration::from_millis(600)).await;
    assert_eq!(server.ready_connections(), 0);
}

#[cfg(target_os = "linux")]
#[tokio::test]
async fn real_seqpacket_socket_roundtrip() {
    use crate::socket::{Listener, SeqPacket};
    let dir = std::env::temp_dir().join(format!("omni-sock-{}", std::process::id()));
    std::fs::create_dir_all(&dir).unwrap();
    let path = dir.join("fsal.sock");
    let listener = Listener::bind(&path).unwrap();
    use std::os::unix::fs::PermissionsExt;
    assert_eq!(
        std::fs::metadata(&path).unwrap().permissions().mode() & 0o777,
        0o600
    );
    // Our own uid and exe stand in for root and ganesha.nfsd.
    // SAFETY: getuid has no preconditions.
    let uid = unsafe { libc::getuid() };
    let exe = std::env::current_exe().unwrap();
    let c = ServerConfig {
        allowed_uids: vec![uid],
        required_exe: Some(exe.to_string_lossy().into_owned()),
        ..cfg()
    };
    let server = Server::new(c, TestHandler::default());
    tokio::spawn(crate::socket::run(listener, server.clone()));
    let mut f = FakeFsal::new(SeqPacket::connect(&path).await.unwrap());
    f.handshake().await;
    f.send(&Message::ReadIntent(intent(Policy::Wait, 0))).await;
    let Message::ReadDecision(d) = f.recv().await.msg else {
        panic!()
    };
    assert_eq!(d.decision, Decision::Delay);
    // A record over 4 KiB is malformed.
    f.send_raw(&vec![0u8; 5000]).await;
    let Message::Error { code, .. } = f.recv().await.msg else {
        panic!()
    };
    assert_eq!(code, error_code::MALFORMED);
    server.shutdown().await;
    let _ = std::fs::remove_dir_all(dir);
}

#[tokio::test]
async fn connection_epoch_moves_on_ready_and_on_loss() {
    let (server, mut f) = start(TestHandler::default(), cfg());
    let e0 = server.connection_epoch();
    f.handshake().await;
    let e1 = server.connection_epoch();
    assert_ne!(e0, e1, "a new ready connection changes the epoch");
    drop(f);
    let deadline = std::time::Instant::now() + Duration::from_secs(5);
    while server.connection_epoch() == e1 {
        assert!(
            std::time::Instant::now() < deadline,
            "loss of a connection must change the epoch"
        );
        tokio::time::sleep(Duration::from_millis(10)).await;
    }
}

/// Partial-version fix (§5.5): the daemon asks a ready FSAL that negotiated
/// `OPEN_STATE` whether NFSv4 clients hold a file open for write, and sums
/// the answers; a silent FSAL is not an answer.
#[tokio::test]
async fn open_writes_asks_every_fsal_that_negotiated_it() {
    let (server, mut f) = start(TestHandler::default(), cfg());
    f.handshake_with(
        1,
        feature::PARK | feature::WRITE_CLOSED | feature::OPEN_STATE,
    )
    .await;
    let s = server.clone();
    let task = tokio::spawn(async move { s.open_writes(obj()).await });
    let fr = f.recv().await;
    assert!(fr.header.req_id & DAEMON_REQ_BIT != 0);
    assert_eq!(fr.msg, Message::OpenStateQuery { obj: obj() });
    f.send_with_id(&Message::OpenState { write_opens: 2 }, fr.header.req_id)
        .await;
    let out = task.await.unwrap();
    assert_eq!((out.asked, out.answered, out.write_opens), (1, 1, 2));
    assert!(!out.closed());

    let s = server.clone();
    let task = tokio::spawn(async move { s.open_writes(obj()).await });
    let fr = f.recv().await;
    f.send_with_id(&Message::OpenState { write_opens: 0 }, fr.header.req_id)
        .await;
    assert!(task.await.unwrap().closed());

    // No answer within the timeout: not closed.
    let out = server.open_writes(obj()).await;
    assert_eq!((out.asked, out.answered), (1, 0));
    assert!(!out.closed());
    assert_eq!(server.pending_acks(), 0, "the waiter is forgotten");
}

#[tokio::test]
async fn open_writes_skips_an_fsal_without_the_feature() {
    let (server, mut f) = start(TestHandler::default(), cfg());
    f.handshake().await;
    let out = server.open_writes(obj()).await;
    assert_eq!((out.asked, out.unsupported), (0, 1));
    assert!(out.closed());
    assert!(f.recv_timeout(Duration::from_millis(200)).await.is_none());
    // And with no FSAL at all, nothing can hold a file open over NFS.
    let none = Server::new(cfg(), TestHandler::default());
    assert!(none.open_writes(obj()).await.closed());
}
