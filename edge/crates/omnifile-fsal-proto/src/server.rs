//! The daemon side of the FSAL socket: handshake, dispatch, pushes, and
//! acknowledged invalidation (`docs/contracts/fsal-socket.md`).
//!
//! Each connection has a reader task and a writer task. The reader answers
//! `PING` itself, so a slow handler never makes the FSAL time the
//! connection out; requests run in their own tasks. Everything the daemon
//! says can only make the FSAL stricter (principle 2): on any internal
//! failure or timeout the reply is the policy's fail-closed answer, and
//! `PARK` is only ever sent when it was negotiated and the range is `ONLINE`.
//!
//! Broken connections are closed rather than repaired. The FSAL then drops
//! its marker cache and pending table and fails closed until it reconnects
//! and is resynchronised, which is always safe.

use std::collections::HashMap;
use std::future::Future;
use std::io;
use std::sync::atomic::{AtomicBool, AtomicU32, AtomicU64, Ordering};
use std::sync::{Arc, Mutex};
use std::time::Duration;

use tokio::sync::{Notify, Semaphore, mpsc, oneshot};
use tracing::{debug, info, warn};

use crate::wire::{
    self, BadMarker, DAEMON_REQ_BIT, Decision, Frame, HelloAck, INVALIDATE_ACK_REQUIRED,
    Invalidate, Message, NsHint, ObjRef, PopulateDir, PopulateResult, ReadDecision, ReadIntent,
    RecallState, WriteClosed, error_code, feature,
};

/// A record transport: one call, one SEQPACKET record.
pub trait PacketIo: Send + Sync + 'static {
    /// Next record; `Ok(None)` at EOF.
    fn recv(&self) -> impl Future<Output = io::Result<Option<Vec<u8>>>> + Send;
    fn send(&self, rec: &[u8]) -> impl Future<Output = io::Result<()>> + Send;
}

/// Who is on the other end, from `SO_PEERCRED` and `HELLO`.
#[derive(Clone, Copy, Debug, Default, PartialEq, Eq)]
pub struct ConnInfo {
    pub conn_id: u64,
    pub pid: i32,
    pub uid: u32,
    /// The FSAL's `HELLO.instance` (random per Ganesha start).
    pub fsal_instance: u64,
    /// Negotiated feature bits.
    pub features: u32,
}

/// FSAL notifications (§5.4). None is needed for correctness.
#[derive(Clone, Debug)]
pub enum Notification {
    WriteClosed(WriteClosed),
    NsHint(NsHint),
    BadMarker(BadMarker),
}

/// The daemon logic behind the socket.
///
/// Handlers must answer quickly (well under the FSAL's 2 s request timeout):
/// start long work (recalls, fills) in the background and answer `DELAY`,
/// `EIO` or `PARK` at once. A handler that overruns `handler_timeout` is
/// cancelled and the FSAL gets the policy's fail-closed answer.
pub trait Handler: Send + Sync + 'static {
    fn read_intent(
        &self,
        conn: ConnInfo,
        req: ReadIntent,
    ) -> impl Future<Output = ReadDecision> + Send;
    fn populate_dir(
        &self,
        conn: ConnInfo,
        req: PopulateDir,
    ) -> impl Future<Output = PopulateResult> + Send;
    /// Must not block.
    fn notification(&self, conn: ConnInfo, n: Notification);
    /// Every active recall, sent between `RESYNC_BEGIN` and `RESYNC_END`.
    fn resync_states(&self) -> Vec<RecallState>;
    fn connected(&self, _conn: ConnInfo) {}
    fn disconnected(&self, _conn: ConnInfo) {}
}

#[derive(Clone, Debug)]
pub struct ServerConfig {
    /// Random per daemon start (`HELLO_ACK.instance`).
    pub instance: u64,
    /// Feature bits this daemon supports.
    pub features: u32,
    pub max_inflight: u32,
    pub park_ms_max: u32,
    pub answer_ttl_ms: u32,
    /// Handler deadline before the fail-closed answer is sent.
    pub handler_timeout: Duration,
    /// Close a connection with no traffic for this long (the FSAL pings
    /// after 1 s idle, so a live one is never this quiet).
    pub idle_timeout: Duration,
    /// How long to wait for every `INVALIDATE_ACK`.
    pub invalidate_timeout: Duration,
    /// Per-connection outgoing queue; a full queue closes the connection.
    pub send_queue: usize,
    /// Peers allowed by `SO_PEERCRED` uid.
    pub allowed_uids: Vec<u32>,
    /// Required `/proc/<pid>/exe`, as a full absolute path (the appliance's
    /// `/opt/ganesha/bin/ganesha.nfsd`). Compared exactly: a binary of the
    /// same name elsewhere is refused, and so is a relative value. `None`
    /// skips the check (tests only).
    pub required_exe: Option<String>,
}

impl Default for ServerConfig {
    fn default() -> Self {
        ServerConfig {
            instance: random_u64(),
            features: feature::PARK
                | feature::CHUNK_MAP
                | feature::POPULATE_DIR
                | feature::NS_HINT
                | feature::WRITE_CLOSED
                | feature::OPEN_STATE,
            max_inflight: 4096,
            park_ms_max: 10_000,
            answer_ttl_ms: 1_000,
            handler_timeout: Duration::from_millis(1_500),
            idle_timeout: Duration::from_secs(5),
            invalidate_timeout: Duration::from_secs(1),
            send_queue: 8192,
            allowed_uids: vec![0],
            required_exe: Some(DEFAULT_GANESHA_EXE.into()),
        }
    }
}

/// Where the appliance installs Ganesha (`dev/scripts/install-ganesha.sh`).
pub const DEFAULT_GANESHA_EXE: &str = "/opt/ganesha/bin/ganesha.nfsd";

/// A random-enough `u64` without a crypto dependency: the instance id only
/// has to differ between daemon starts.
pub fn random_u64() -> u64 {
    use std::hash::{BuildHasher, Hasher};
    let mut h = std::collections::hash_map::RandomState::new().build_hasher();
    h.write_u128(
        std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .map(|d| d.as_nanos())
            .unwrap_or(0),
    );
    h.write_u32(std::process::id());
    h.finish() | 1
}

/// Result of an acknowledged invalidation (§5.5, R1 §5.1 step 4).
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct InvalidateOutcome {
    pub inval_seq: u64,
    /// Connections past `HELLO_ACK` when the invalidation was sent.
    pub connections: usize,
    /// Accepted connections still before `HELLO_ACK` (or between `HELLO_ACK`
    /// and being marked ready). They were not asked, so they never count as
    /// acknowledged.
    pub not_ready: usize,
    pub acked: usize,
}

impl InvalidateOutcome {
    /// True only when at least one FSAL is connected, none is half-way
    /// through its handshake, and every one acknowledged. Only then may the
    /// daemon punch holes. Zero connections is **not** an ack: a Ganesha
    /// that is restarting (or not yet up) is indistinguishable from one
    /// whose connection was lost, and the daemon must not punch on the
    /// strength of silence. An edge with no Ganesha at all says so in its
    /// configuration and checks [`InvalidateOutcome::no_fsal`] itself.
    pub fn all_acked(&self) -> bool {
        self.connections > 0 && self.not_ready == 0 && self.acked == self.connections
    }

    /// No FSAL connection of any kind existed when the invalidation was sent.
    pub fn no_fsal(&self) -> bool {
        self.connections == 0 && self.not_ready == 0
    }
}

/// Whether NFSv4 clients hold a file open for write (§5.5,
/// `OPEN_STATE_QUERY`), as every ready FSAL answered.
#[derive(Clone, Copy, Debug, Default, PartialEq, Eq)]
pub struct OpenWrites {
    /// Ready connections asked (they negotiated `OPEN_STATE`).
    pub asked: usize,
    /// Of those, the ones that answered in time.
    pub answered: usize,
    /// NFSv4 share states open for write, summed over the answers.
    pub write_opens: u64,
    /// Ready connections that did not negotiate `OPEN_STATE` (an older
    /// FSAL): they could not be asked.
    pub unsupported: usize,
    /// Accepted connections not yet past their handshake.
    pub not_ready: usize,
    /// Of the FSALs asked, how long ago the newest Ganesha instance first
    /// connected to this daemon. After a Ganesha restart (or a daemon
    /// restart: an instance it has not seen), NFSv4 clients reclaim their
    /// opens during grace, so until the lease and grace are over an
    /// answer of 0 may only mean "not reclaimed yet".
    pub newest_instance_age: Option<Duration>,
}

impl OpenWrites {
    /// True when every FSAL that could be asked answered, none is in its
    /// handshake, and no NFSv4 client holds the file open for write. With
    /// no FSAL at all this is true: no NFS client can hold a file open
    /// without a Ganesha.
    pub fn closed(&self) -> bool {
        self.write_opens == 0 && self.answered == self.asked && self.not_ready == 0
    }
}

enum Out {
    Rec(Vec<u8>),
    /// Send, then close the connection.
    RecThenClose(Vec<u8>),
}

struct Conn {
    info: Mutex<ConnInfo>,
    tx: mpsc::Sender<Out>,
    ready: AtomicBool,
    features: AtomicU32,
    acks: Mutex<HashMap<u64, oneshot::Sender<u64>>>,
    kill: Notify,
    dead: AtomicBool,
    inflight: Semaphore,
    /// The server's connection epoch, bumped when this connection becomes
    /// ready and again when it stops being ready.
    epoch: Arc<AtomicU64>,
}

impl Conn {
    fn info(&self) -> ConnInfo {
        *self.info.lock().unwrap()
    }

    fn close(&self) {
        self.dead.store(true, Ordering::SeqCst);
        if self.ready.swap(false, Ordering::SeqCst) {
            self.epoch.fetch_add(1, Ordering::SeqCst);
        }
        // Waiters for acks see their sender dropped: not acknowledged.
        self.acks.lock().unwrap().clear();
        self.kill.notify_waiters();
        self.kill.notify_one();
    }

    /// Queue a record; a full or closed queue closes the connection.
    fn queue(&self, out: Out) -> bool {
        match self.tx.try_send(out) {
            Ok(()) => true,
            Err(e) => {
                warn!(conn = self.info().conn_id, err = %e, "fsal send queue unusable, closing connection");
                self.close();
                false
            }
        }
    }
}

struct Shared<H> {
    cfg: ServerConfig,
    handler: H,
    conns: Mutex<HashMap<u64, Arc<Conn>>>,
    next_conn: AtomicU64,
    next_req: AtomicU64,
    next_inval: AtomicU64,
    shutting_down: AtomicBool,
    epoch: Arc<AtomicU64>,
    /// When each Ganesha instance (`HELLO.instance`) first became ready on
    /// this server: a new instance's NFSv4 clients may still be reclaiming
    /// their opens (`OpenWrites::newest_instance_age`).
    instances: Mutex<HashMap<u64, std::time::Instant>>,
}

/// The server. Cheap to clone; all clones share the connections.
pub struct Server<H> {
    shared: Arc<Shared<H>>,
}

impl<H> Clone for Server<H> {
    fn clone(&self) -> Self {
        Server {
            shared: self.shared.clone(),
        }
    }
}

impl<H: Handler> Server<H> {
    pub fn new(cfg: ServerConfig, handler: H) -> Self {
        Server {
            shared: Arc::new(Shared {
                cfg,
                handler,
                conns: Mutex::new(HashMap::new()),
                next_conn: AtomicU64::new(1),
                next_req: AtomicU64::new(1),
                next_inval: AtomicU64::new(1),
                shutting_down: AtomicBool::new(false),
                epoch: Arc::new(AtomicU64::new(0)),
                instances: Mutex::new(HashMap::new()),
            }),
        }
    }

    pub fn config(&self) -> &ServerConfig {
        &self.shared.cfg
    }

    pub fn handler(&self) -> &H {
        &self.shared.handler
    }

    /// The features every ready connection negotiated (their AND), or
    /// `None` with no ready connection.
    pub fn common_features(&self) -> Option<u32> {
        let conns = self.ready_conns();
        if conns.is_empty() {
            return None;
        }
        Some(
            conns
                .iter()
                .fold(u32::MAX, |a, c| a & c.features.load(Ordering::SeqCst)),
        )
    }

    /// Connections past `HELLO_ACK`.
    pub fn ready_connections(&self) -> usize {
        self.ready_conns().len()
    }

    /// The peer pids (`SO_PEERCRED`) of the connections past `HELLO_ACK`.
    pub fn ready_pids(&self) -> Vec<i32> {
        self.ready_conns().iter().map(|c| c.info().pid).collect()
    }

    /// Accepted, live connections not (yet) past `HELLO_ACK`: an FSAL
    /// half-way through its handshake.
    pub fn handshaking_connections(&self) -> usize {
        self.not_ready_conns()
    }

    /// `INVALIDATE_ACK` waiters across all connections (tests, metrics).
    pub fn pending_acks(&self) -> usize {
        self.shared
            .conns
            .lock()
            .unwrap()
            .values()
            .map(|c| c.acks.lock().unwrap().len())
            .sum()
    }

    /// Changes whenever an FSAL connection becomes ready or stops being
    /// ready. A fill fenced at one epoch stops when it changes.
    pub fn connection_epoch(&self) -> u64 {
        self.shared.epoch.load(Ordering::SeqCst)
    }

    pub fn is_shutting_down(&self) -> bool {
        self.shared.shutting_down.load(Ordering::SeqCst)
    }

    /// Accepted, live connections not (yet) past `HELLO_ACK`.
    fn not_ready_conns(&self) -> usize {
        self.shared
            .conns
            .lock()
            .unwrap()
            .values()
            .filter(|c| !c.ready.load(Ordering::SeqCst) && !c.dead.load(Ordering::SeqCst))
            .count()
    }

    fn ready_conns(&self) -> Vec<Arc<Conn>> {
        self.shared
            .conns
            .lock()
            .unwrap()
            .values()
            .filter(|c| c.ready.load(Ordering::SeqCst) && !c.dead.load(Ordering::SeqCst))
            .cloned()
            .collect()
    }

    fn daemon_req_id(&self) -> u64 {
        self.shared.next_req.fetch_add(1, Ordering::Relaxed) | DAEMON_REQ_BIT
    }

    /// Push a `RECALL_STATE` to every connected FSAL.
    pub fn push_recall_state(&self, st: RecallState) {
        let rec = wire::encode(&Message::RecallState(st), 0);
        for c in self.ready_conns() {
            c.queue(Out::Rec(rec.clone()));
        }
    }

    /// Send `INVALIDATE` to every connected FSAL. With `ack_required`, wait up
    /// to `invalidate_timeout` for every `INVALIDATE_ACK`. A connection that
    /// closes while we wait counts as not acknowledged: the caller must not
    /// narrow, and retries later.
    pub async fn invalidate(
        &self,
        obj: ObjRef,
        what: u32,
        ack_required: bool,
    ) -> InvalidateOutcome {
        let inval_seq = self.shared.next_inval.fetch_add(1, Ordering::Relaxed);
        let not_ready = self.not_ready_conns();
        let conns = self.ready_conns();
        let mut waits = Vec::with_capacity(conns.len());
        for c in &conns {
            let req_id = self.daemon_req_id();
            let rx = if ack_required {
                let (tx, rx) = oneshot::channel();
                c.acks.lock().unwrap().insert(req_id, tx);
                Some(rx)
            } else {
                None
            };
            let msg = Message::Invalidate(Invalidate {
                obj,
                what,
                flags: if ack_required {
                    INVALIDATE_ACK_REQUIRED
                } else {
                    0
                },
                inval_seq,
            });
            let sent = c.queue(Out::Rec(wire::encode(&msg, req_id)));
            if let Some(rx) = rx {
                waits.push((c.clone(), req_id, sent, rx));
            }
        }
        if !ack_required {
            return InvalidateOutcome {
                inval_seq,
                connections: conns.len(),
                not_ready,
                acked: conns.len(),
            };
        }
        let deadline = tokio::time::Instant::now() + self.shared.cfg.invalidate_timeout;
        let mut acked = 0;
        for (c, req_id, sent, rx) in waits {
            let got = if sent {
                tokio::time::timeout_at(deadline, rx).await.ok()
            } else {
                None
            };
            match got {
                Some(Ok(seq)) if seq == inval_seq => acked += 1,
                // Timed out, wrong seq or never sent: forget the waiter so a
                // late ack finds nothing and the table does not grow.
                _ => {
                    c.acks.lock().unwrap().remove(&req_id);
                }
            }
        }
        let out = InvalidateOutcome {
            inval_seq,
            connections: conns.len(),
            not_ready,
            acked,
        };
        if !out.all_acked() {
            warn!(
                ?obj,
                inval_seq,
                connections = out.connections,
                acked,
                "invalidate not acknowledged by every FSAL"
            );
        }
        out
    }

    /// Ask every ready FSAL whether NFSv4 clients hold `obj` open for write
    /// (`OPEN_STATE_QUERY`, §5.5), waiting up to `invalidate_timeout` for
    /// the answers. A connection that closes or stays silent counts as not
    /// answered.
    pub async fn open_writes(&self, obj: ObjRef) -> OpenWrites {
        let mut out = OpenWrites {
            not_ready: self.not_ready_conns(),
            ..OpenWrites::default()
        };
        let mut waits = Vec::new();
        for c in self.ready_conns() {
            if c.features.load(Ordering::SeqCst) & feature::OPEN_STATE == 0 {
                out.unsupported += 1;
                continue;
            }
            let req_id = self.daemon_req_id();
            let (tx, rx) = oneshot::channel();
            c.acks.lock().unwrap().insert(req_id, tx);
            let sent = c.queue(Out::Rec(wire::encode(
                &Message::OpenStateQuery { obj },
                req_id,
            )));
            out.asked += 1;
            waits.push((c, req_id, sent, rx));
        }
        {
            let seen = self.shared.instances.lock().unwrap();
            out.newest_instance_age = waits
                .iter()
                .filter_map(|(c, ..)| seen.get(&c.info().fsal_instance))
                .map(|t| t.elapsed())
                .min();
        }
        let deadline = tokio::time::Instant::now() + self.shared.cfg.invalidate_timeout;
        for (c, req_id, sent, rx) in waits {
            let got = if sent {
                tokio::time::timeout_at(deadline, rx).await.ok()
            } else {
                None
            };
            match got {
                Some(Ok(n)) => {
                    out.answered += 1;
                    out.write_opens += n;
                }
                _ => {
                    c.acks.lock().unwrap().remove(&req_id);
                }
            }
        }
        if out.answered < out.asked {
            warn!(
                ?obj,
                asked = out.asked,
                answered = out.answered,
                "OPEN_STATE_QUERY not answered by every FSAL"
            );
        }
        out
    }

    /// Tell every FSAL we are going (`ERROR` 5), then close. The FSAL goes
    /// to its disconnected state and fails closed. New connections are refused.
    pub async fn shutdown(&self) {
        self.shared.shutting_down.store(true, Ordering::SeqCst);
        let conns: Vec<Arc<Conn>> = self
            .shared
            .conns
            .lock()
            .unwrap()
            .values()
            .cloned()
            .collect();
        let rec = wire::encode(
            &Message::Error {
                code: error_code::SHUTTING_DOWN,
                message: b"daemon shutting down".to_vec(),
            },
            0,
        );
        for c in &conns {
            if !c.queue(Out::RecThenClose(rec.clone())) {
                c.close();
            }
        }
        // Give writers a moment to flush the ERROR, then force-close.
        let deadline = tokio::time::Instant::now() + Duration::from_millis(500);
        while tokio::time::Instant::now() < deadline
            && !self.shared.conns.lock().unwrap().is_empty()
        {
            tokio::time::sleep(Duration::from_millis(10)).await;
        }
        for c in self.shared.conns.lock().unwrap().values() {
            c.close();
        }
    }

    /// Serve one accepted connection until it closes.
    pub async fn serve<T: PacketIo>(
        &self,
        io: T,
        peer_pid: i32,
        peer_uid: u32,
        peer_exe: Option<String>,
    ) {
        let io = Arc::new(io);
        let conn_id = self.shared.next_conn.fetch_add(1, Ordering::Relaxed);
        let (tx, rx) = mpsc::channel(self.shared.cfg.send_queue.max(1));
        let conn = Arc::new(Conn {
            info: Mutex::new(ConnInfo {
                conn_id,
                pid: peer_pid,
                uid: peer_uid,
                ..Default::default()
            }),
            tx,
            ready: AtomicBool::new(false),
            features: AtomicU32::new(0),
            acks: Mutex::new(HashMap::new()),
            kill: Notify::new(),
            dead: AtomicBool::new(false),
            inflight: Semaphore::new(self.shared.cfg.max_inflight.max(1) as usize),
            epoch: self.shared.epoch.clone(),
        });

        let writer = tokio::spawn(writer_task(io.clone(), rx, conn.clone()));

        let allowed = self.peer_allowed(peer_uid, peer_exe.as_deref());
        if !allowed || self.is_shutting_down() {
            let (code, why) = if allowed {
                (error_code::SHUTTING_DOWN, "daemon shutting down")
            } else {
                warn!(
                    conn_id,
                    peer_pid,
                    peer_uid,
                    ?peer_exe,
                    "fsal peer not allowed"
                );
                (error_code::PEER_NOT_ALLOWED, "peer not allowed")
            };
            conn.queue(Out::RecThenClose(wire::encode(
                &Message::Error {
                    code,
                    message: why.as_bytes().to_vec(),
                },
                0,
            )));
            let _ = writer.await;
            return;
        }

        self.shared
            .conns
            .lock()
            .unwrap()
            .insert(conn_id, conn.clone());
        info!(conn_id, peer_pid, "fsal connected");
        self.reader_loop(&io, &conn).await;
        // Let a queued ERROR reach the peer, then drop the connection.
        let mut writer = writer;
        if tokio::time::timeout(Duration::from_millis(200), &mut writer)
            .await
            .is_err()
        {
            writer.abort();
        }
        conn.close();
        self.shared.conns.lock().unwrap().remove(&conn_id);
        if conn.features.load(Ordering::SeqCst) != 0 || conn.info().fsal_instance != 0 {
            self.shared.handler.disconnected(conn.info());
        }
        info!(conn_id, "fsal disconnected");
    }

    fn peer_allowed(&self, uid: u32, exe: Option<&str>) -> bool {
        if !self.shared.cfg.allowed_uids.contains(&uid) {
            return false;
        }
        match (&self.shared.cfg.required_exe, exe) {
            (None, _) => true,
            (Some(want), Some(have)) => {
                // `/proc/<pid>/exe` is already canonical; a configured value
                // that is not absolute matches nothing (fail closed).
                std::path::Path::new(want).is_absolute() && have == want
            }
            (Some(_), None) => false,
        }
    }

    async fn reader_loop<T: PacketIo>(&self, io: &Arc<T>, conn: &Arc<Conn>) {
        let idle = self.shared.cfg.idle_timeout;
        loop {
            if conn.dead.load(Ordering::SeqCst) {
                return;
            }
            let rec = tokio::select! {
                r = io.recv() => r,
                _ = conn.kill.notified() => return,
                _ = tokio::time::sleep(idle) => {
                    warn!(conn = conn.info().conn_id, "fsal connection idle, closing");
                    return;
                }
            };
            let rec = match rec {
                Ok(Some(rec)) => rec,
                Ok(None) => return,
                Err(e) => {
                    debug!(conn = conn.info().conn_id, err = %e, "fsal recv error");
                    return;
                }
            };
            let frame = match wire::decode(&rec) {
                Ok(f) => f,
                Err(e) => {
                    warn!(conn = conn.info().conn_id, err = %e, "malformed message from fsal");
                    self.error_and_close(conn, error_code::MALFORMED, &e.to_string());
                    return;
                }
            };
            if !self.dispatch(conn, frame) {
                return;
            }
        }
    }

    fn error_and_close(&self, conn: &Conn, code: u32, why: &str) {
        conn.queue(Out::RecThenClose(wire::encode(
            &Message::Error {
                code,
                message: why.as_bytes().to_vec(),
            },
            0,
        )));
    }

    /// Handle one message. Returns false to close the connection.
    fn dispatch(&self, conn: &Arc<Conn>, f: Frame) -> bool {
        let ready = conn.ready.load(Ordering::SeqCst);
        let req_id = f.header.req_id;
        match f.msg {
            Message::Hello(h) => {
                if ready {
                    self.error_and_close(conn, error_code::MALFORMED, "HELLO after HELLO_ACK");
                    return false;
                }
                if h.proto_major != wire::PROTO_MAJOR {
                    warn!(
                        conn = conn.info().conn_id,
                        major = h.proto_major,
                        "fsal protocol major mismatch"
                    );
                    self.error_and_close(
                        conn,
                        error_code::VERSION_MISMATCH,
                        "protocol major mismatch",
                    );
                    return false;
                }
                let proto_minor = h.proto_minor.min(wire::PROTO_MINOR);
                let mut features = h.features & self.shared.cfg.features;
                if proto_minor < 2 {
                    // A v0.1 FSAL: maps ignored, prefix only (fsal-socket.md §9).
                    features &= !wire::V02_FEATURES;
                }
                {
                    let mut info = conn.info.lock().unwrap();
                    info.fsal_instance = h.instance;
                    info.features = features;
                }
                conn.features.store(features, Ordering::SeqCst);
                let ack = HelloAck {
                    proto_major: wire::PROTO_MAJOR,
                    proto_minor,
                    features,
                    instance: self.shared.cfg.instance,
                    max_inflight: self.shared.cfg.max_inflight,
                    park_ms_max: self.shared.cfg.park_ms_max,
                    answer_ttl_ms: self.shared.cfg.answer_ttl_ms,
                };
                info!(
                    conn = conn.info().conn_id,
                    ganesha = %String::from_utf8_lossy(&h.ganesha_version),
                    fsal_build = %String::from_utf8_lossy(&h.fsal_build),
                    features,
                    "fsal hello"
                );
                if !conn.queue(Out::Rec(wire::encode(&Message::HelloAck(ack), req_id))) {
                    return false;
                }
                // Ready before the resync list is taken: a push racing with
                // the resync is either in the list or queued after it.
                {
                    let mut seen = self.shared.instances.lock().unwrap();
                    if seen.len() >= 64 {
                        // Old instances: only the newest few matter.
                        let oldest = seen.iter().min_by_key(|e| *e.1).map(|e| *e.0);
                        if let Some(k) = oldest {
                            seen.remove(&k);
                        }
                    }
                    seen.entry(conn.info().fsal_instance)
                        .or_insert_with(std::time::Instant::now);
                }
                conn.ready.store(true, Ordering::SeqCst);
                self.shared.epoch.fetch_add(1, Ordering::SeqCst);
                self.shared.handler.connected(conn.info());
                conn.queue(Out::Rec(wire::encode(&Message::ResyncBegin, 0)));
                for st in self.shared.handler.resync_states() {
                    conn.queue(Out::Rec(wire::encode(&Message::RecallState(st), 0)));
                }
                conn.queue(Out::Rec(wire::encode(&Message::ResyncEnd, 0)))
            }
            _ if !ready => {
                self.error_and_close(conn, error_code::MALFORMED, "message before HELLO");
                false
            }
            Message::Ping { mono_ns } => {
                conn.queue(Out::Rec(wire::encode(&Message::Pong { mono_ns }, req_id)))
            }
            Message::Pong { .. } => true,
            Message::InvalidateAck { inval_seq } => {
                if let Some(tx) = conn.acks.lock().unwrap().remove(&req_id) {
                    let _ = tx.send(inval_seq);
                }
                true
            }
            Message::OpenState { write_opens } => {
                if let Some(tx) = conn.acks.lock().unwrap().remove(&req_id) {
                    let _ = tx.send(u64::from(write_opens));
                }
                true
            }
            Message::ReadIntent(ri) => {
                let Ok(permit) = conn.inflight.try_acquire() else {
                    let d = ReadDecision::simple(
                        ri.policy.fail_closed(),
                        wire::reason::OVER_WAITER_CAP,
                    );
                    return conn.queue(Out::Rec(wire::encode(&Message::ReadDecision(d), req_id)));
                };
                permit.forget();
                let this = self.clone();
                let conn = conn.clone();
                tokio::spawn(async move {
                    let info = conn.info();
                    let fail = ri.policy.fail_closed();
                    let d = match tokio::time::timeout(
                        this.shared.cfg.handler_timeout,
                        this.shared.handler.read_intent(info, ri.clone()),
                    )
                    .await
                    {
                        Ok(d) => d,
                        Err(_) => {
                            warn!(
                                conn = info.conn_id,
                                file_id = ri.obj.file_id,
                                "read_intent handler timed out"
                            );
                            ReadDecision::simple(fail, wire::reason::NONE)
                        }
                    };
                    let d = sanitize_decision(d, &ri, info.features);
                    conn.queue(Out::Rec(wire::encode(&Message::ReadDecision(d), req_id)));
                    conn.inflight.add_permits(1);
                });
                true
            }
            Message::PopulateDir(pd) => {
                let this = self.clone();
                let conn = conn.clone();
                tokio::spawn(async move {
                    let info = conn.info();
                    let r = tokio::time::timeout(
                        this.shared.cfg.handler_timeout,
                        this.shared.handler.populate_dir(info, pd),
                    )
                    .await
                    .unwrap_or(PopulateResult {
                        result: 3,
                        eta_ms: 0,
                    });
                    conn.queue(Out::Rec(wire::encode(&Message::PopulateResult(r), req_id)));
                });
                true
            }
            Message::WriteClosed(w) => {
                self.shared
                    .handler
                    .notification(conn.info(), Notification::WriteClosed(w));
                true
            }
            Message::NsHint(n) => {
                self.shared
                    .handler
                    .notification(conn.info(), Notification::NsHint(n));
                true
            }
            Message::BadMarker(b) => {
                warn!(conn = conn.info().conn_id, ?b.obj, reason = b.reason, errno = b.errno, "fsal reported a bad marker");
                self.shared
                    .handler
                    .notification(conn.info(), Notification::BadMarker(b));
                true
            }
            Message::Error { code, message } => {
                warn!(conn = conn.info().conn_id, code, msg = %String::from_utf8_lossy(&message), "fsal sent ERROR");
                !error_code::closes(code)
            }
            Message::Unknown { ty, .. } => {
                if ty & wire::REPLY_BIT == 0 && req_id != 0 {
                    conn.queue(Out::Rec(wire::encode(
                        &Message::Error {
                            code: error_code::UNKNOWN_REQUEST,
                            message: b"unknown request".to_vec(),
                        },
                        req_id,
                    )))
                } else {
                    debug!(
                        conn = conn.info().conn_id,
                        ty, "ignoring unknown notification"
                    );
                    true
                }
            }
            // Messages only the daemon sends; a late reply is ignored.
            other => {
                debug!(
                    conn = conn.info().conn_id,
                    msg = other.name(),
                    "ignoring unexpected message"
                );
                true
            }
        }
    }
}

/// Enforce the daemon-side fail-closed rules on a handler's answer:
/// - an unknown decision code becomes the policy default;
/// - `PARK` needs the negotiated `PARK` feature and an `ONLINE` marker, and
///   is never used for fill-first ops;
/// - `RECHECK` is passed through (the FSAL re-reads the marker regardless).
pub fn sanitize_decision(mut d: ReadDecision, ri: &ReadIntent, features: u32) -> ReadDecision {
    const ONLINE: u16 = 1;
    let fail = ri.policy.fail_closed();
    match d.decision {
        Decision::Recheck | Decision::Delay | Decision::Eio => {}
        Decision::Park => {
            if features & feature::PARK == 0
                || ri.marker_flags & ONLINE == 0
                || ri.op.needs_full_fill()
            {
                d.decision = fail;
            }
        }
        Decision::Other(_) => d.decision = fail,
    }
    d
}

async fn writer_task<T: PacketIo>(io: Arc<T>, mut rx: mpsc::Receiver<Out>, conn: Arc<Conn>) {
    while let Some(out) = rx.recv().await {
        let (rec, then_close) = match out {
            Out::Rec(r) => (r, false),
            Out::RecThenClose(r) => (r, true),
        };
        if let Err(e) = io.send(&rec).await {
            debug!(conn = conn.info().conn_id, err = %e, "fsal send failed");
            conn.close();
            return;
        }
        if then_close {
            conn.close();
            return;
        }
    }
}
