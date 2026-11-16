//! Binary codecs for every message of `docs/contracts/fsal-socket.md` §3–§5.
//!
//! Little-endian, naturally aligned fixed parts, then the message's strings
//! as `u16 len` + bytes. One SEQPACKET record is one message, at most
//! [`MAX_MESSAGE`] bytes. The encoder never builds a larger one: strings are
//! cut and the header's `TRUNCATED` flag is set.

use std::fmt;

pub const HEADER_LEN: usize = 16;
pub const MAX_MESSAGE: usize = 4096;
/// `path_hint` is at most this long; longer paths are cut from the left.
pub const MAX_PATH_HINT: usize = 1024;

pub const FLAG_TRUNCATED: u16 = 1 << 0;
/// Bit 15 of `type`: the message is a reply.
pub const REPLY_BIT: u16 = 0x8000;
/// Daemon-originated request ids have bit 63 set.
pub const DAEMON_REQ_BIT: u64 = 1 << 63;

pub const HELLO_MAGIC: u32 = 0x5346_4D4F;
pub const PROTO_MAJOR: u16 = 0;
/// v0.2 (M5): `CHUNK_MAP` is used (the FSAL reads `trusted.omnifile.map`,
/// one `READ_INTENT` per chunk, every `FETCHING` push resumes parked reads).
/// A peer that negotiates v0.1 never gets it; `POPULATE_DIR` stays a v0.1
/// feature (`fsal-socket.md` §9).
pub const PROTO_MINOR: u16 = 2;
/// The features only a v0.2 peer may be given.
pub const V02_FEATURES: u32 = feature::CHUNK_MAP;

/// Message type codes (§5).
pub mod ty {
    pub const HELLO: u16 = 0x0001;
    pub const HELLO_ACK: u16 = 0x8001;
    pub const READ_INTENT: u16 = 0x0010;
    pub const READ_DECISION: u16 = 0x8010;
    pub const POPULATE_DIR: u16 = 0x0011;
    pub const POPULATE_RESULT: u16 = 0x8011;
    pub const WRITE_CLOSED: u16 = 0x0020;
    pub const NS_HINT: u16 = 0x0021;
    pub const BAD_MARKER: u16 = 0x0022;
    pub const PING: u16 = 0x0030;
    pub const PONG: u16 = 0x8030;
    pub const RECALL_STATE: u16 = 0x0040;
    pub const INVALIDATE: u16 = 0x0041;
    pub const INVALIDATE_ACK: u16 = 0x8041;
    pub const RESYNC_BEGIN: u16 = 0x0042;
    pub const RESYNC_END: u16 = 0x0043;
    pub const OPEN_STATE_QUERY: u16 = 0x0044;
    pub const OPEN_STATE: u16 = 0x8044;
    pub const ERROR: u16 = 0x7FFF;
}

/// `HELLO` feature bits (§5.1).
pub mod feature {
    pub const PARK: u32 = 1 << 0;
    pub const CHUNK_MAP: u32 = 1 << 1;
    pub const POPULATE_DIR: u32 = 1 << 2;
    pub const NS_HINT: u32 = 1 << 3;
    pub const WRITE_CLOSED: u32 = 1 << 4;
    /// `OPEN_STATE_QUERY` / `OPEN_STATE` (§5.5).
    pub const OPEN_STATE: u32 = 1 << 5;
}

/// `ERROR` codes (§5.6).
pub mod error_code {
    pub const VERSION_MISMATCH: u32 = 1;
    pub const UNKNOWN_REQUEST: u32 = 2;
    pub const MALFORMED: u32 = 3;
    pub const PEER_NOT_ALLOWED: u32 = 4;
    pub const SHUTTING_DOWN: u32 = 5;

    /// Codes after which the sender closes the connection.
    pub fn closes(code: u32) -> bool {
        matches!(
            code,
            VERSION_MISMATCH | MALFORMED | PEER_NOT_ALLOWED | SHUTTING_DOWN
        )
    }
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct Header {
    pub ty: u16,
    pub flags: u16,
    pub length: u32,
    pub req_id: u64,
}

impl Header {
    pub fn is_reply(&self) -> bool {
        self.ty & REPLY_BIT != 0
    }
}

/// `obj_ref` (§4.1), 32 bytes.
#[derive(Clone, Copy, Debug, Default, PartialEq, Eq, Hash)]
pub struct ObjRef {
    pub fsid: u64,
    pub ino: u64,
    pub ino_gen: u32,
    pub export_id: u32,
    pub file_id: u64,
}

/// `identity.origin`.
#[derive(Clone, Copy, Debug, PartialEq, Eq, Hash)]
#[repr(u8)]
pub enum Origin {
    Request = 0,
    OpenStateid = 1,
    LastOpener = 2,
    Unknown = 3,
}

impl Origin {
    fn from_u8(v: u8) -> Origin {
        match v {
            0 => Origin::Request,
            1 => Origin::OpenStateid,
            2 => Origin::LastOpener,
            _ => Origin::Unknown,
        }
    }
}

/// `identity` (§4.2), 48 bytes. `principal` travels as a string of the
/// enclosing message.
#[derive(Clone, Copy, Debug, PartialEq, Eq, Hash)]
pub struct Identity {
    /// `0xFFFFFFFF` = unknown.
    pub uid: u32,
    pub gid: u32,
    pub origin: Origin,
    pub nfs_vers: u8,
    pub nfs_minor: u8,
    /// 0 unknown; 1 sys; 2 krb5; 3 krb5i; 4 krb5p.
    pub sec: u8,
    /// 0 none; 4 IPv4; 6 IPv6.
    pub addr_family: u8,
    pub addr: [u8; 16],
    pub port: u16,
    pub clientid: u64,
}

impl Default for Identity {
    fn default() -> Self {
        Identity {
            uid: u32::MAX,
            gid: u32::MAX,
            origin: Origin::Unknown,
            nfs_vers: 0,
            nfs_minor: 0,
            sec: 0,
            addr_family: 0,
            addr: [0; 16],
            port: 0,
            clientid: 0,
        }
    }
}

impl Identity {
    pub const UNKNOWN_UID: u32 = u32::MAX;

    /// `proof` for the control API: Kerberos proves the uid, `sec=sys` only claims it.
    pub fn proven(&self) -> bool {
        matches!(self.sec, 2..=4)
    }

    pub fn sec_name(&self) -> &'static str {
        match self.sec {
            1 => "sys",
            2 => "krb5",
            3 => "krb5i",
            4 => "krb5p",
            _ => "unknown",
        }
    }

    pub fn client_addr(&self) -> Option<std::net::IpAddr> {
        match self.addr_family {
            4 => Some(std::net::IpAddr::from([
                self.addr[0],
                self.addr[1],
                self.addr[2],
                self.addr[3],
            ])),
            6 => Some(std::net::IpAddr::from(self.addr)),
            _ => None,
        }
    }
}

/// `READ_INTENT.op`.
#[derive(Clone, Copy, Debug, PartialEq, Eq, Hash)]
pub enum ReadOp {
    Read,
    ReadPlus,
    CopySrc,
    Write,
    SetattrSize,
    Fallocate,
    Other(u8),
}

impl ReadOp {
    fn from_u8(v: u8) -> ReadOp {
        match v {
            1 => ReadOp::Read,
            2 => ReadOp::ReadPlus,
            3 => ReadOp::CopySrc,
            4 => ReadOp::Write,
            5 => ReadOp::SetattrSize,
            6 => ReadOp::Fallocate,
            o => ReadOp::Other(o),
        }
    }
    fn to_u8(self) -> u8 {
        match self {
            ReadOp::Read => 1,
            ReadOp::ReadPlus => 2,
            ReadOp::CopySrc => 3,
            ReadOp::Write => 4,
            ReadOp::SetattrSize => 5,
            ReadOp::Fallocate => 6,
            ReadOp::Other(o) => o,
        }
    }
    /// Ops that need the whole file local before they may proceed (R1 §6).
    pub fn needs_full_fill(self) -> bool {
        matches!(
            self,
            ReadOp::Write | ReadOp::SetattrSize | ReadOp::Fallocate
        )
    }
}

/// The export's open policy (`policy` byte, §10).
#[derive(Clone, Copy, Debug, PartialEq, Eq, Hash)]
pub enum Policy {
    Wait,
    FailFast,
    WaitQuick,
    Other(u8),
}

impl Policy {
    fn from_u8(v: u8) -> Policy {
        match v {
            1 => Policy::Wait,
            2 => Policy::FailFast,
            3 => Policy::WaitQuick,
            o => Policy::Other(o),
        }
    }
    fn to_u8(self) -> u8 {
        match self {
            Policy::Wait => 1,
            Policy::FailFast => 2,
            Policy::WaitQuick => 3,
            Policy::Other(o) => o,
        }
    }
    /// The fail-closed answer for a non-local range (§7): `EIO` on failfast,
    /// `DELAY` otherwise. An unknown policy fails fast.
    pub fn fail_closed(self) -> Decision {
        match self {
            Policy::Wait | Policy::WaitQuick => Decision::Delay,
            Policy::FailFast | Policy::Other(_) => Decision::Eio,
        }
    }
    /// From the catalog's `on_open` word (`wait`, `fail`, `auto`).
    pub fn from_on_open(on_open: &str) -> Policy {
        match on_open {
            "wait" => Policy::Wait,
            "auto" => Policy::WaitQuick,
            _ => Policy::FailFast,
        }
    }
}

/// `READ_DECISION.decision`.
#[derive(Clone, Copy, Debug, PartialEq, Eq, Hash)]
pub enum Decision {
    Recheck,
    Delay,
    Eio,
    Park,
    Other(u8),
}

impl Decision {
    fn from_u8(v: u8) -> Decision {
        match v {
            1 => Decision::Recheck,
            2 => Decision::Delay,
            3 => Decision::Eio,
            4 => Decision::Park,
            o => Decision::Other(o),
        }
    }
    pub fn to_u8(self) -> u8 {
        match self {
            Decision::Recheck => 1,
            Decision::Delay => 2,
            Decision::Eio => 3,
            Decision::Park => 4,
            Decision::Other(o) => o,
        }
    }
}

/// `READ_DECISION.reason`.
pub mod reason {
    pub const NONE: u8 = 0;
    pub const RECALL_STARTED: u8 = 1;
    pub const RECALL_IN_PROGRESS: u8 = 2;
    pub const FETCH_IN_PROGRESS: u8 = 3;
    pub const SERVICE_UNREACHABLE: u8 = 4;
    pub const NO_CREDENTIAL: u8 = 5;
    pub const UNKNOWN_FILE: u8 = 6;
    pub const OVER_WAITER_CAP: u8 = 7;
    pub const RESTORE_FAILED: u8 = 8;
    pub const TOO_SLOW: u8 = 9;
    /// M5: the service sent a backend or credential mode this edge doesn't
    /// know: "upgrade the edge" (decision 35 F4).
    pub const UNSUPPORTED_BACKEND: u8 = 10;
    /// M5: the source is not readable by the edge; the read waits for a
    /// copy into the home backend. Informational, like every reason.
    pub const SOURCE_NOT_READABLE: u8 = 11;
}

/// `RECALL_STATE.state` (§5.5).
#[derive(Clone, Copy, Debug, PartialEq, Eq, Hash)]
pub enum RecallPhase {
    Queued,
    Restoring,
    Copying,
    Fetching,
    Done,
    Failed,
    Cancelled,
    Blocked,
    Other(u8),
}

impl RecallPhase {
    fn from_u8(v: u8) -> RecallPhase {
        match v {
            1 => RecallPhase::Queued,
            2 => RecallPhase::Restoring,
            3 => RecallPhase::Copying,
            4 => RecallPhase::Fetching,
            5 => RecallPhase::Done,
            6 => RecallPhase::Failed,
            7 => RecallPhase::Cancelled,
            8 => RecallPhase::Blocked,
            o => RecallPhase::Other(o),
        }
    }
    fn to_u8(self) -> u8 {
        match self {
            RecallPhase::Queued => 1,
            RecallPhase::Restoring => 2,
            RecallPhase::Copying => 3,
            RecallPhase::Fetching => 4,
            RecallPhase::Done => 5,
            RecallPhase::Failed => 6,
            RecallPhase::Cancelled => 7,
            RecallPhase::Blocked => 8,
            RecallPhase::Other(o) => o,
        }
    }
    /// From the catalog's `recall.state` word (§5.5 mapping table).
    pub fn from_catalog(state: &str) -> RecallPhase {
        match state {
            "requested" => RecallPhase::Queued,
            "rehydrating" => RecallPhase::Restoring,
            "copying" => RecallPhase::Copying,
            "downloading" => RecallPhase::Fetching,
            "available" => RecallPhase::Done,
            "failed" => RecallPhase::Failed,
            "cancelled" => RecallPhase::Cancelled,
            // The file was replaced or deleted while it was recalled (control
            // API §4.4): the recall stopped. To a reader it is as a cancelled
            // one: the policy's answer, and the next read looks again.
            "superseded" => RecallPhase::Cancelled,
            _ => RecallPhase::Blocked,
        }
    }
    pub fn is_terminal(self) -> bool {
        matches!(
            self,
            RecallPhase::Done | RecallPhase::Failed | RecallPhase::Cancelled
        )
    }
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Hello {
    pub proto_major: u16,
    pub proto_minor: u16,
    pub features: u32,
    pub pid: u32,
    pub instance: u64,
    pub ganesha_version: Vec<u8>,
    pub fsal_build: Vec<u8>,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct HelloAck {
    pub proto_major: u16,
    pub proto_minor: u16,
    pub features: u32,
    pub instance: u64,
    pub max_inflight: u32,
    pub park_ms_max: u32,
    pub answer_ttl_ms: u32,
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct ReadIntent {
    pub obj: ObjRef,
    pub version_id: u64,
    pub offset: u64,
    pub length: u64,
    pub size: u64,
    pub resident_bytes: u64,
    pub op: ReadOp,
    pub policy: Policy,
    pub marker_flags: u16,
    pub quick_ms: u32,
    pub parked: bool,
    pub identity: Identity,
    pub principal: Vec<u8>,
    pub path_hint: Vec<u8>,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct ReadDecision {
    pub decision: Decision,
    pub reason: u8,
    /// `u32::MAX` = unknown.
    pub eta_s: u32,
    pub ttl_ms: u32,
    pub recall_id: u64,
    pub resident_bytes: u64,
}

impl ReadDecision {
    pub const ETA_UNKNOWN: u32 = u32::MAX;

    pub fn simple(decision: Decision, reason: u8) -> ReadDecision {
        ReadDecision {
            decision,
            reason,
            eta_s: Self::ETA_UNKNOWN,
            ttl_ms: 0,
            recall_id: 0,
            resident_bytes: 0,
        }
    }
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct PopulateDir {
    pub obj: ObjRef,
    pub identity: Identity,
    pub principal: Vec<u8>,
    pub path_hint: Vec<u8>,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct PopulateResult {
    /// 1 DONE; 2 IN_PROGRESS; 3 EIO.
    pub result: u8,
    pub eta_ms: u32,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct WriteClosed {
    pub obj: ObjRef,
    pub size_after: u64,
    pub identity: Identity,
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct NsHint {
    /// 1 CREATE … 8 RMDIR.
    pub op: u8,
    /// `SETATTR` only (the first reserved byte, §5.4): bit 0 `SIZE` (a
    /// truncate to 0), bit 1 `OWNER` (owner or group). 0 from an FSAL that
    /// predates it, which sent `SETATTR` only for a truncate.
    pub what: u8,
    pub parent: ObjRef,
    pub target: ObjRef,
    pub identity: Identity,
    pub principal: Vec<u8>,
    pub name: Vec<u8>,
    pub new_name: Vec<u8>,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct BadMarker {
    pub obj: ObjRef,
    pub reason: u32,
    pub errno: u32,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct RecallState {
    pub obj: ObjRef,
    pub version_id: u64,
    pub recall_id: u64,
    pub state: RecallPhase,
    /// 2 `DELAY` or 3 `EIO`.
    pub answer: Decision,
    pub eta_s: u32,
    pub resident_bytes: u64,
    pub valid_ms: u32,
}

/// `INVALIDATE.what` bits.
pub mod what {
    pub const MARKER: u32 = 1 << 0;
    pub const ATTRS: u32 = 1 << 1;
    pub const CONTENT: u32 = 1 << 2;
    pub const DIR: u32 = 1 << 3;
}

pub const INVALIDATE_ACK_REQUIRED: u32 = 1 << 0;

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct Invalidate {
    pub obj: ObjRef,
    pub what: u32,
    pub flags: u32,
    pub inval_seq: u64,
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub enum Message {
    Hello(Hello),
    HelloAck(HelloAck),
    ReadIntent(ReadIntent),
    ReadDecision(ReadDecision),
    PopulateDir(PopulateDir),
    PopulateResult(PopulateResult),
    WriteClosed(WriteClosed),
    NsHint(NsHint),
    BadMarker(BadMarker),
    Ping {
        mono_ns: u64,
    },
    Pong {
        mono_ns: u64,
    },
    RecallState(RecallState),
    Invalidate(Invalidate),
    InvalidateAck {
        inval_seq: u64,
    },
    /// No fixed part.
    ResyncBegin,
    /// No fixed part.
    ResyncEnd,
    /// Daemon → FSAL request (§5.5): is `obj` open for write over NFSv4?
    OpenStateQuery {
        obj: ObjRef,
    },
    /// The FSAL's reply: NFSv4 share states open for write on `obj`.
    OpenState {
        write_opens: u32,
    },
    Error {
        code: u32,
        message: Vec<u8>,
    },
    /// A type this side does not know; its body is kept for logging.
    Unknown {
        ty: u16,
        body: Vec<u8>,
    },
}

impl Message {
    pub fn ty(&self) -> u16 {
        match self {
            Message::Hello(_) => ty::HELLO,
            Message::HelloAck(_) => ty::HELLO_ACK,
            Message::ReadIntent(_) => ty::READ_INTENT,
            Message::ReadDecision(_) => ty::READ_DECISION,
            Message::PopulateDir(_) => ty::POPULATE_DIR,
            Message::PopulateResult(_) => ty::POPULATE_RESULT,
            Message::WriteClosed(_) => ty::WRITE_CLOSED,
            Message::NsHint(_) => ty::NS_HINT,
            Message::BadMarker(_) => ty::BAD_MARKER,
            Message::Ping { .. } => ty::PING,
            Message::Pong { .. } => ty::PONG,
            Message::RecallState(_) => ty::RECALL_STATE,
            Message::Invalidate(_) => ty::INVALIDATE,
            Message::InvalidateAck { .. } => ty::INVALIDATE_ACK,
            Message::ResyncBegin => ty::RESYNC_BEGIN,
            Message::ResyncEnd => ty::RESYNC_END,
            Message::OpenStateQuery { .. } => ty::OPEN_STATE_QUERY,
            Message::OpenState { .. } => ty::OPEN_STATE,
            Message::Error { .. } => ty::ERROR,
            Message::Unknown { ty, .. } => *ty,
        }
    }

    pub fn name(&self) -> &'static str {
        match self {
            Message::Hello(_) => "HELLO",
            Message::HelloAck(_) => "HELLO_ACK",
            Message::ReadIntent(_) => "READ_INTENT",
            Message::ReadDecision(_) => "READ_DECISION",
            Message::PopulateDir(_) => "POPULATE_DIR",
            Message::PopulateResult(_) => "POPULATE_RESULT",
            Message::WriteClosed(_) => "WRITE_CLOSED",
            Message::NsHint(_) => "NS_HINT",
            Message::BadMarker(_) => "BAD_MARKER",
            Message::Ping { .. } => "PING",
            Message::Pong { .. } => "PONG",
            Message::RecallState(_) => "RECALL_STATE",
            Message::Invalidate(_) => "INVALIDATE",
            Message::InvalidateAck { .. } => "INVALIDATE_ACK",
            Message::ResyncBegin => "RESYNC_BEGIN",
            Message::ResyncEnd => "RESYNC_END",
            Message::OpenStateQuery { .. } => "OPEN_STATE_QUERY",
            Message::OpenState { .. } => "OPEN_STATE",
            Message::Error { .. } => "ERROR",
            Message::Unknown { .. } => "UNKNOWN",
        }
    }

    /// The fixed-part size of a known type in protocol 0.1.
    pub fn fixed_len(ty: u16) -> Option<usize> {
        Some(match ty {
            ty::HELLO | ty::HELLO_ACK => 32,
            ty::READ_INTENT => 136,
            ty::READ_DECISION => 40,
            ty::POPULATE_DIR => 80,
            ty::POPULATE_RESULT => 8,
            ty::WRITE_CLOSED => 88,
            ty::NS_HINT => 120,
            ty::BAD_MARKER => 40,
            ty::PING | ty::PONG => 8,
            ty::RECALL_STATE => 72,
            ty::INVALIDATE => 48,
            ty::INVALIDATE_ACK => 8,
            ty::RESYNC_BEGIN | ty::RESYNC_END => 0,
            ty::OPEN_STATE_QUERY => 32,
            ty::OPEN_STATE => 8,
            ty::ERROR => 4,
            _ => return None,
        })
    }
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub enum DecodeError {
    /// Shorter than the 16-byte header.
    ShortHeader(usize),
    /// `length` differs from the record size: the receiver closes.
    LengthMismatch { header: u32, record: usize },
    /// Over [`MAX_MESSAGE`].
    TooLong(usize),
    /// The fixed part is shorter than the contract's: malformed.
    ShortFixed { ty: u16, have: usize, need: usize },
    /// A string runs past the end of the record.
    BadString { ty: u16 },
}

impl fmt::Display for DecodeError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            DecodeError::ShortHeader(n) => {
                write!(f, "record of {n} bytes is shorter than the header")
            }
            DecodeError::LengthMismatch { header, record } => {
                write!(f, "header length {header} != record size {record}")
            }
            DecodeError::TooLong(n) => write!(f, "record of {n} bytes exceeds {MAX_MESSAGE}"),
            DecodeError::ShortFixed { ty, have, need } => {
                write!(f, "type {ty:#06x}: fixed part {have} bytes, need {need}")
            }
            DecodeError::BadString { ty } => write!(f, "type {ty:#06x}: string overruns record"),
        }
    }
}

impl std::error::Error for DecodeError {}

/// A decoded record.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Frame {
    pub header: Header,
    pub msg: Message,
}

// ---------------------------------------------------------------- encoding

struct W {
    b: Vec<u8>,
}

impl W {
    fn u8(&mut self, v: u8) {
        self.b.push(v);
    }
    fn u16(&mut self, v: u16) {
        self.b.extend_from_slice(&v.to_le_bytes());
    }
    fn u32(&mut self, v: u32) {
        self.b.extend_from_slice(&v.to_le_bytes());
    }
    fn u64(&mut self, v: u64) {
        self.b.extend_from_slice(&v.to_le_bytes());
    }
    fn zeros(&mut self, n: usize) {
        self.b.resize(self.b.len() + n, 0);
    }
    fn obj(&mut self, o: &ObjRef) {
        self.u64(o.fsid);
        self.u64(o.ino);
        self.u32(o.ino_gen);
        self.u32(o.export_id);
        self.u64(o.file_id);
    }
    fn identity(&mut self, i: &Identity) {
        self.u32(i.uid);
        self.u32(i.gid);
        self.u8(i.origin as u8);
        self.u8(i.nfs_vers);
        self.u8(i.nfs_minor);
        self.u8(i.sec);
        self.u8(i.addr_family);
        self.zeros(3);
        self.b.extend_from_slice(&i.addr);
        self.u16(i.port);
        self.zeros(2 + 4);
        self.u64(i.clientid);
    }
}

/// How a string is shortened when it doesn't fit.
#[derive(Clone, Copy)]
enum Cut {
    /// Keep the tail (paths: the file name matters most).
    Left,
    /// Keep the head.
    Right,
}

/// A string field: its bytes, how to cut it, and its cap.
type StrField<'a> = (&'a [u8], Cut, usize);

fn fixed_part(msg: &Message) -> (Vec<u8>, Vec<StrField<'_>>) {
    let mut w = W {
        b: Vec::with_capacity(160),
    };
    let unlimited = u16::MAX as usize;
    let strings: Vec<StrField<'_>> = match msg {
        Message::Hello(h) => {
            w.u32(HELLO_MAGIC);
            w.u16(h.proto_major);
            w.u16(h.proto_minor);
            w.u32(h.features);
            w.u32(h.pid);
            w.u64(h.instance);
            w.zeros(8);
            vec![
                (&h.ganesha_version, Cut::Right, unlimited),
                (&h.fsal_build, Cut::Right, unlimited),
            ]
        }
        Message::HelloAck(a) => {
            w.u16(a.proto_major);
            w.u16(a.proto_minor);
            w.u32(a.features);
            w.u64(a.instance);
            w.u32(a.max_inflight);
            w.u32(a.park_ms_max);
            w.u32(a.answer_ttl_ms);
            w.zeros(4);
            vec![]
        }
        Message::ReadIntent(r) => {
            w.obj(&r.obj);
            w.u64(r.version_id);
            w.u64(r.offset);
            w.u64(r.length);
            w.u64(r.size);
            w.u64(r.resident_bytes);
            w.u8(r.op.to_u8());
            w.u8(r.policy.to_u8());
            w.u16(r.marker_flags);
            w.u32(r.quick_ms);
            w.u8(r.parked as u8);
            w.zeros(7);
            w.identity(&r.identity);
            vec![
                (&r.principal, Cut::Right, unlimited),
                (&r.path_hint, Cut::Left, MAX_PATH_HINT),
            ]
        }
        Message::ReadDecision(d) => {
            w.u8(d.decision.to_u8());
            w.u8(d.reason);
            w.zeros(2);
            w.u32(d.eta_s);
            w.u32(d.ttl_ms);
            w.zeros(4);
            w.u64(d.recall_id);
            w.u64(d.resident_bytes);
            w.zeros(8);
            vec![]
        }
        Message::PopulateDir(p) => {
            w.obj(&p.obj);
            w.identity(&p.identity);
            vec![
                (&p.principal, Cut::Right, unlimited),
                (&p.path_hint, Cut::Left, MAX_PATH_HINT),
            ]
        }
        Message::PopulateResult(p) => {
            w.u8(p.result);
            w.zeros(3);
            w.u32(p.eta_ms);
            vec![]
        }
        Message::WriteClosed(c) => {
            w.obj(&c.obj);
            w.u64(c.size_after);
            w.identity(&c.identity);
            vec![]
        }
        Message::NsHint(n) => {
            w.u8(n.op);
            w.u8(n.what);
            w.zeros(6);
            w.obj(&n.parent);
            w.obj(&n.target);
            w.identity(&n.identity);
            vec![
                (&n.principal, Cut::Right, unlimited),
                (&n.name, Cut::Right, unlimited),
                (&n.new_name, Cut::Right, unlimited),
            ]
        }
        Message::BadMarker(m) => {
            w.obj(&m.obj);
            w.u32(m.reason);
            w.u32(m.errno);
            vec![]
        }
        Message::Ping { mono_ns } | Message::Pong { mono_ns } => {
            w.u64(*mono_ns);
            vec![]
        }
        Message::RecallState(s) => {
            w.obj(&s.obj);
            w.u64(s.version_id);
            w.u64(s.recall_id);
            w.u8(s.state.to_u8());
            w.u8(s.answer.to_u8());
            w.zeros(2);
            w.u32(s.eta_s);
            w.u64(s.resident_bytes);
            w.u32(s.valid_ms);
            w.zeros(4);
            vec![]
        }
        Message::Invalidate(i) => {
            w.obj(&i.obj);
            w.u32(i.what);
            w.u32(i.flags);
            w.u64(i.inval_seq);
            vec![]
        }
        Message::InvalidateAck { inval_seq } => {
            w.u64(*inval_seq);
            vec![]
        }
        Message::ResyncBegin | Message::ResyncEnd => vec![],
        Message::OpenStateQuery { obj } => {
            w.obj(obj);
            vec![]
        }
        Message::OpenState { write_opens } => {
            w.u32(*write_opens);
            w.zeros(4);
            vec![]
        }
        Message::Error { code, message } => {
            w.u32(*code);
            vec![(message, Cut::Right, unlimited)]
        }
        Message::Unknown { body, .. } => {
            w.b.extend_from_slice(body);
            vec![]
        }
    };
    (w.b, strings)
}

/// Encode one record. Never longer than [`MAX_MESSAGE`]: strings are cut
/// (paths from the left) and `TRUNCATED` is set when anything was cut.
pub fn encode(msg: &Message, req_id: u64) -> Vec<u8> {
    let (fixed, strings) = fixed_part(msg);
    let mut truncated = false;
    let mut lens: Vec<usize> = strings
        .iter()
        .map(|(s, _, cap)| {
            if s.len() > *cap {
                truncated = true;
                *cap
            } else {
                s.len()
            }
        })
        .collect();
    let total =
        |lens: &[usize]| HEADER_LEN + fixed.len() + lens.iter().map(|l| 2 + l).sum::<usize>();
    // Shrink strings in message order (principal before path_hint, so the
    // path, which matters most in logs and note files, survives longest).
    for i in 0..lens.len() {
        if total(&lens) <= MAX_MESSAGE {
            break;
        }
        let over = total(&lens) - MAX_MESSAGE;
        let cut = over.min(lens[i]);
        if cut > 0 {
            lens[i] -= cut;
            truncated = true;
        }
    }
    let len = total(&lens);
    let mut out = Vec::with_capacity(len);
    out.extend_from_slice(&msg.ty().to_le_bytes());
    out.extend_from_slice(&(if truncated { FLAG_TRUNCATED } else { 0 }).to_le_bytes());
    out.extend_from_slice(&(len as u32).to_le_bytes());
    out.extend_from_slice(&req_id.to_le_bytes());
    out.extend_from_slice(&fixed);
    for ((s, cut, _), &l) in strings.iter().zip(&lens) {
        out.extend_from_slice(&(l as u16).to_le_bytes());
        match cut {
            Cut::Right => out.extend_from_slice(&s[..l]),
            Cut::Left => out.extend_from_slice(&s[s.len() - l..]),
        }
    }
    debug_assert_eq!(out.len(), len);
    out
}

// ---------------------------------------------------------------- decoding

struct R<'a> {
    b: &'a [u8],
    p: usize,
}

impl R<'_> {
    fn u8(&mut self) -> u8 {
        let v = self.b[self.p];
        self.p += 1;
        v
    }
    fn u16(&mut self) -> u16 {
        let v = u16::from_le_bytes([self.b[self.p], self.b[self.p + 1]]);
        self.p += 2;
        v
    }
    fn u32(&mut self) -> u32 {
        let v = u32::from_le_bytes(self.b[self.p..self.p + 4].try_into().unwrap());
        self.p += 4;
        v
    }
    fn u64(&mut self) -> u64 {
        let v = u64::from_le_bytes(self.b[self.p..self.p + 8].try_into().unwrap());
        self.p += 8;
        v
    }
    fn skip(&mut self, n: usize) {
        self.p += n;
    }
    fn obj(&mut self) -> ObjRef {
        ObjRef {
            fsid: self.u64(),
            ino: self.u64(),
            ino_gen: self.u32(),
            export_id: self.u32(),
            file_id: self.u64(),
        }
    }
    fn identity(&mut self) -> Identity {
        let uid = self.u32();
        let gid = self.u32();
        let origin = Origin::from_u8(self.u8());
        let nfs_vers = self.u8();
        let nfs_minor = self.u8();
        let sec = self.u8();
        let addr_family = self.u8();
        self.skip(3);
        let mut addr = [0u8; 16];
        addr.copy_from_slice(&self.b[self.p..self.p + 16]);
        self.p += 16;
        let port = self.u16();
        self.skip(6);
        let clientid = self.u64();
        Identity {
            uid,
            gid,
            origin,
            nfs_vers,
            nfs_minor,
            sec,
            addr_family,
            addr,
            port,
            clientid,
        }
    }
}

/// Strings after the fixed part.
struct S<'a> {
    b: &'a [u8],
    p: usize,
    ty: u16,
}

impl S<'_> {
    fn string(&mut self) -> Result<Vec<u8>, DecodeError> {
        if self.p + 2 > self.b.len() {
            return Err(DecodeError::BadString { ty: self.ty });
        }
        let l = u16::from_le_bytes([self.b[self.p], self.b[self.p + 1]]) as usize;
        self.p += 2;
        if self.p + l > self.b.len() {
            return Err(DecodeError::BadString { ty: self.ty });
        }
        let s = self.b[self.p..self.p + l].to_vec();
        self.p += l;
        Ok(s)
    }
}

/// Decode one record. A fixed part longer than this side knows is accepted
/// and its extra bytes skipped (§3); one shorter is malformed.
pub fn decode(rec: &[u8]) -> Result<Frame, DecodeError> {
    if rec.len() < HEADER_LEN {
        return Err(DecodeError::ShortHeader(rec.len()));
    }
    if rec.len() > MAX_MESSAGE {
        return Err(DecodeError::TooLong(rec.len()));
    }
    let header = Header {
        ty: u16::from_le_bytes([rec[0], rec[1]]),
        flags: u16::from_le_bytes([rec[2], rec[3]]),
        length: u32::from_le_bytes(rec[4..8].try_into().unwrap()),
        req_id: u64::from_le_bytes(rec[8..16].try_into().unwrap()),
    };
    if header.length as usize != rec.len() {
        return Err(DecodeError::LengthMismatch {
            header: header.length,
            record: rec.len(),
        });
    }
    let body = &rec[HEADER_LEN..];
    let Some(need) = Message::fixed_len(header.ty) else {
        return Ok(Frame {
            header,
            msg: Message::Unknown {
                ty: header.ty,
                body: body.to_vec(),
            },
        });
    };
    if body.len() < need {
        return Err(DecodeError::ShortFixed {
            ty: header.ty,
            have: body.len(),
            need,
        });
    }
    let mut r = R { b: body, p: 0 };
    // Strings follow the fixed part as this version defines it.
    let mut s = S {
        b: body,
        p: need,
        ty: header.ty,
    };
    let msg = match header.ty {
        ty::HELLO => {
            let magic = r.u32();
            if magic != HELLO_MAGIC {
                // A wrong magic is a malformed HELLO.
                return Err(DecodeError::ShortFixed {
                    ty: header.ty,
                    have: 0,
                    need,
                });
            }
            let proto_major = r.u16();
            let proto_minor = r.u16();
            let features = r.u32();
            let pid = r.u32();
            let instance = r.u64();
            // A newer FSAL's HELLO may carry a longer fixed part, whose size
            // this side cannot know (§3: it is computed from the version,
            // and HELLO is what negotiates it). Its strings start at an
            // unknown offset: skip them rather than misparse fixed-part
            // bytes as strings. They are informational only.
            let (ganesha_version, fsal_build) =
                if proto_major == PROTO_MAJOR && proto_minor > PROTO_MINOR {
                    (Vec::new(), Vec::new())
                } else {
                    (s.string()?, s.string()?)
                };
            Message::Hello(Hello {
                proto_major,
                proto_minor,
                features,
                pid,
                instance,
                ganesha_version,
                fsal_build,
            })
        }
        ty::HELLO_ACK => {
            let proto_major = r.u16();
            let proto_minor = r.u16();
            let features = r.u32();
            let instance = r.u64();
            let max_inflight = r.u32();
            let park_ms_max = r.u32();
            let answer_ttl_ms = r.u32();
            Message::HelloAck(HelloAck {
                proto_major,
                proto_minor,
                features,
                instance,
                max_inflight,
                park_ms_max,
                answer_ttl_ms,
            })
        }
        ty::READ_INTENT => {
            let obj = r.obj();
            let version_id = r.u64();
            let offset = r.u64();
            let length = r.u64();
            let size = r.u64();
            let resident_bytes = r.u64();
            let op = ReadOp::from_u8(r.u8());
            let policy = Policy::from_u8(r.u8());
            let marker_flags = r.u16();
            let quick_ms = r.u32();
            let parked = r.u8() != 0;
            r.skip(7);
            let identity = r.identity();
            Message::ReadIntent(ReadIntent {
                obj,
                version_id,
                offset,
                length,
                size,
                resident_bytes,
                op,
                policy,
                marker_flags,
                quick_ms,
                parked,
                identity,
                principal: s.string()?,
                path_hint: s.string()?,
            })
        }
        ty::READ_DECISION => {
            let decision = Decision::from_u8(r.u8());
            let reason = r.u8();
            r.skip(2);
            let eta_s = r.u32();
            let ttl_ms = r.u32();
            r.skip(4);
            let recall_id = r.u64();
            let resident_bytes = r.u64();
            Message::ReadDecision(ReadDecision {
                decision,
                reason,
                eta_s,
                ttl_ms,
                recall_id,
                resident_bytes,
            })
        }
        ty::POPULATE_DIR => {
            let obj = r.obj();
            let identity = r.identity();
            Message::PopulateDir(PopulateDir {
                obj,
                identity,
                principal: s.string()?,
                path_hint: s.string()?,
            })
        }
        ty::POPULATE_RESULT => {
            let result = r.u8();
            r.skip(3);
            Message::PopulateResult(PopulateResult {
                result,
                eta_ms: r.u32(),
            })
        }
        ty::WRITE_CLOSED => {
            let obj = r.obj();
            let size_after = r.u64();
            Message::WriteClosed(WriteClosed {
                obj,
                size_after,
                identity: r.identity(),
            })
        }
        ty::NS_HINT => {
            let op = r.u8();
            let what = r.u8();
            r.skip(6);
            let parent = r.obj();
            let target = r.obj();
            let identity = r.identity();
            Message::NsHint(NsHint {
                op,
                what,
                parent,
                target,
                identity,
                principal: s.string()?,
                name: s.string()?,
                // §5.4: `new_name` is sent for RENAME only; FSAL_OMNI sends
                // two strings for every other op.
                new_name: if s.p < s.b.len() {
                    s.string()?
                } else {
                    Vec::new()
                },
            })
        }
        ty::BAD_MARKER => {
            let obj = r.obj();
            let reason = r.u32();
            Message::BadMarker(BadMarker {
                obj,
                reason,
                errno: r.u32(),
            })
        }
        ty::PING => Message::Ping { mono_ns: r.u64() },
        ty::PONG => Message::Pong { mono_ns: r.u64() },
        ty::RECALL_STATE => {
            let obj = r.obj();
            let version_id = r.u64();
            let recall_id = r.u64();
            let state = RecallPhase::from_u8(r.u8());
            let answer = Decision::from_u8(r.u8());
            r.skip(2);
            let eta_s = r.u32();
            let resident_bytes = r.u64();
            let valid_ms = r.u32();
            Message::RecallState(RecallState {
                obj,
                version_id,
                recall_id,
                state,
                answer,
                eta_s,
                resident_bytes,
                valid_ms,
            })
        }
        ty::INVALIDATE => {
            let obj = r.obj();
            let what = r.u32();
            let flags = r.u32();
            Message::Invalidate(Invalidate {
                obj,
                what,
                flags,
                inval_seq: r.u64(),
            })
        }
        ty::INVALIDATE_ACK => Message::InvalidateAck { inval_seq: r.u64() },
        ty::RESYNC_BEGIN => Message::ResyncBegin,
        ty::RESYNC_END => Message::ResyncEnd,
        ty::OPEN_STATE_QUERY => Message::OpenStateQuery { obj: r.obj() },
        ty::OPEN_STATE => Message::OpenState {
            write_opens: r.u32(),
        },
        ty::ERROR => {
            let code = r.u32();
            Message::Error {
                code,
                message: s.string()?,
            }
        }
        _ => unreachable!("fixed_len covers every known type"),
    };
    Ok(Frame { header, msg })
}

#[cfg(test)]
mod tests {
    use super::*;
    use proptest::prelude::*;

    fn obj() -> impl Strategy<Value = ObjRef> {
        (
            any::<u64>(),
            any::<u64>(),
            any::<u32>(),
            any::<u32>(),
            any::<u64>(),
        )
            .prop_map(|(fsid, ino, ino_gen, export_id, file_id)| ObjRef {
                fsid,
                ino,
                ino_gen,
                export_id,
                file_id,
            })
    }

    fn identity() -> impl Strategy<Value = Identity> {
        (
            any::<u32>(),
            any::<u32>(),
            0u8..4,
            any::<[u8; 5]>(),
            any::<[u8; 16]>(),
            any::<u16>(),
            any::<u64>(),
        )
            .prop_map(|(uid, gid, o, b, addr, port, clientid)| Identity {
                uid,
                gid,
                origin: Origin::from_u8(o),
                nfs_vers: b[0],
                nfs_minor: b[1],
                sec: b[2],
                addr_family: b[3],
                addr,
                port,
                clientid,
            })
    }

    fn bytes(max: usize) -> impl Strategy<Value = Vec<u8>> {
        prop::collection::vec(any::<u8>(), 0..max)
    }

    fn message() -> impl Strategy<Value = Message> {
        prop_oneof![
            (
                any::<[u16; 2]>(),
                any::<u32>(),
                any::<u32>(),
                any::<u64>(),
                bytes(40),
                bytes(40)
            )
                .prop_map(|(v, features, pid, instance, gv, fb)| Message::Hello(
                    Hello {
                        proto_major: v[0],
                        proto_minor: v[1],
                        features,
                        pid,
                        instance,
                        ganesha_version: gv,
                        fsal_build: fb,
                    }
                )),
            (any::<[u16; 2]>(), any::<[u32; 4]>(), any::<u64>()).prop_map(|(v, n, instance)| {
                Message::HelloAck(HelloAck {
                    proto_major: v[0],
                    proto_minor: v[1],
                    features: n[0],
                    instance,
                    max_inflight: n[1],
                    park_ms_max: n[2],
                    answer_ttl_ms: n[3],
                })
            }),
            (
                obj(),
                any::<[u64; 5]>(),
                any::<[u8; 3]>(),
                any::<u16>(),
                any::<u32>(),
                identity(),
                bytes(64),
                bytes(900)
            )
                .prop_map(
                    |(obj, n, b, marker_flags, quick_ms, identity, principal, path_hint)| {
                        Message::ReadIntent(ReadIntent {
                            obj,
                            version_id: n[0],
                            offset: n[1],
                            length: n[2],
                            size: n[3],
                            resident_bytes: n[4],
                            op: ReadOp::from_u8(b[0]),
                            policy: Policy::from_u8(b[1]),
                            marker_flags,
                            quick_ms,
                            parked: b[2] & 1 == 1,
                            identity,
                            principal,
                            path_hint,
                        })
                    }
                ),
            (any::<[u8; 2]>(), any::<[u32; 2]>(), any::<[u64; 2]>()).prop_map(|(b, n, l)| {
                Message::ReadDecision(ReadDecision {
                    decision: Decision::from_u8(b[0]),
                    reason: b[1],
                    eta_s: n[0],
                    ttl_ms: n[1],
                    recall_id: l[0],
                    resident_bytes: l[1],
                })
            }),
            (obj(), identity(), bytes(32), bytes(200)).prop_map(
                |(obj, identity, principal, path_hint)| {
                    Message::PopulateDir(PopulateDir {
                        obj,
                        identity,
                        principal,
                        path_hint,
                    })
                }
            ),
            (any::<u8>(), any::<u32>()).prop_map(|(result, eta_ms)| Message::PopulateResult(
                PopulateResult { result, eta_ms }
            )),
            (obj(), any::<u64>(), identity()).prop_map(|(obj, size_after, identity)| {
                Message::WriteClosed(WriteClosed {
                    obj,
                    size_after,
                    identity,
                })
            }),
            (
                any::<(u8, u8)>(),
                obj(),
                obj(),
                identity(),
                bytes(20),
                bytes(100),
                bytes(100)
            )
                .prop_map(
                    |((op, what), parent, target, identity, principal, name, new_name)| {
                        Message::NsHint(NsHint {
                            op,
                            what,
                            parent,
                            target,
                            identity,
                            principal,
                            name,
                            new_name,
                        })
                    }
                ),
            (obj(), any::<u32>(), any::<u32>()).prop_map(
                |(obj, reason, errno)| Message::BadMarker(BadMarker { obj, reason, errno })
            ),
            any::<u64>().prop_map(|mono_ns| Message::Ping { mono_ns }),
            any::<u64>().prop_map(|mono_ns| Message::Pong { mono_ns }),
            (
                obj(),
                any::<[u64; 3]>(),
                any::<[u8; 2]>(),
                any::<[u32; 2]>()
            )
                .prop_map(|(obj, l, b, n)| {
                    Message::RecallState(RecallState {
                        obj,
                        version_id: l[0],
                        recall_id: l[1],
                        state: RecallPhase::from_u8(b[0]),
                        answer: Decision::from_u8(b[1]),
                        eta_s: n[0],
                        resident_bytes: l[2],
                        valid_ms: n[1],
                    })
                }),
            (obj(), any::<u32>(), any::<u32>(), any::<u64>()).prop_map(
                |(obj, what, flags, inval_seq)| {
                    Message::Invalidate(Invalidate {
                        obj,
                        what,
                        flags,
                        inval_seq,
                    })
                }
            ),
            any::<u64>().prop_map(|inval_seq| Message::InvalidateAck { inval_seq }),
            Just(Message::ResyncBegin),
            Just(Message::ResyncEnd),
            obj().prop_map(|obj| Message::OpenStateQuery { obj }),
            any::<u32>().prop_map(|write_opens| Message::OpenState { write_opens }),
            (any::<u32>(), bytes(200)).prop_map(|(code, message)| Message::Error { code, message }),
            (0x1000u16..0x1100, bytes(64)).prop_map(|(ty, body)| Message::Unknown { ty, body }),
        ]
    }

    proptest! {
        #[test]
        fn roundtrip(msg in message(), req_id in any::<u64>()) {
            let rec = encode(&msg, req_id);
            prop_assert!(rec.len() <= MAX_MESSAGE);
            let f = decode(&rec).unwrap();
            prop_assert_eq!(f.header.req_id, req_id);
            prop_assert_eq!(f.header.length as usize, rec.len());
            prop_assert_eq!(f.header.flags & FLAG_TRUNCATED, 0);
            prop_assert_eq!(f.msg, msg);
        }

        #[test]
        fn decode_never_panics(rec in prop::collection::vec(any::<u8>(), 0..300)) {
            let _ = decode(&rec);
        }

        #[test]
        fn decode_of_valid_header_with_random_body_never_panics(
            ty in prop::sample::select(vec![
                ty::HELLO, ty::READ_INTENT, ty::POPULATE_DIR, ty::NS_HINT, ty::ERROR, ty::RECALL_STATE,
            ]),
            body in prop::collection::vec(any::<u8>(), 0..400),
        ) {
            let mut rec = Vec::new();
            rec.extend_from_slice(&ty.to_le_bytes());
            rec.extend_from_slice(&0u16.to_le_bytes());
            rec.extend_from_slice(&((HEADER_LEN + body.len()) as u32).to_le_bytes());
            rec.extend_from_slice(&1u64.to_le_bytes());
            rec.extend_from_slice(&body);
            let _ = decode(&rec);
        }

        #[test]
        fn longer_fixed_part_is_skipped(extra in prop::collection::vec(any::<u8>(), 1..32), v in any::<u64>()) {
            // A newer sender appends fields to a string-less message.
            let mut rec = encode(&Message::InvalidateAck { inval_seq: v }, 9);
            rec.extend_from_slice(&extra);
            let l = rec.len() as u32;
            rec[4..8].copy_from_slice(&l.to_le_bytes());
            prop_assert_eq!(decode(&rec).unwrap().msg, Message::InvalidateAck { inval_seq: v });
        }
    }

    /// FSAL_OMNI sends NS_HINT with two strings (principal, name) for every
    /// op but RENAME (§5.4; `omni_build_ns_hint`). Found by the M1 e2e test:
    /// the daemon dropped every SETATTR hint as malformed.
    #[test]
    fn ns_hint_without_new_name_decodes() {
        let msg = Message::NsHint(NsHint {
            op: 4,
            what: 1,
            parent: ObjRef::default(),
            target: ObjRef::default(),
            identity: Identity::default(),
            principal: Vec::new(),
            name: Vec::new(),
            new_name: Vec::new(),
        });
        let mut rec = encode(&msg, 0);
        rec.truncate(rec.len() - 2); // drop the empty new_name's u16 length
        let len = rec.len() as u32;
        rec[4..8].copy_from_slice(&len.to_le_bytes());
        assert_eq!(decode(&rec).unwrap().msg, msg);
    }

    #[test]
    fn fixed_sizes_match_contract() {
        for (msg, want) in [
            (
                Message::ReadDecision(ReadDecision::simple(Decision::Delay, 0)),
                40,
            ),
            (Message::InvalidateAck { inval_seq: 1 }, 8),
            (Message::ResyncBegin, 0),
            (
                Message::OpenStateQuery {
                    obj: ObjRef::default(),
                },
                32,
            ),
            (Message::OpenState { write_opens: 2 }, 8),
            (
                Message::RecallState(RecallState {
                    obj: ObjRef::default(),
                    version_id: 0,
                    recall_id: 0,
                    state: RecallPhase::Queued,
                    answer: Decision::Delay,
                    eta_s: 0,
                    resident_bytes: 0,
                    valid_ms: 0,
                }),
                72,
            ),
            (
                Message::Invalidate(Invalidate {
                    obj: ObjRef::default(),
                    what: 1,
                    flags: 1,
                    inval_seq: 1,
                }),
                48,
            ),
        ] {
            assert_eq!(encode(&msg, 0).len(), HEADER_LEN + want, "{}", msg.name());
            assert_eq!(Message::fixed_len(msg.ty()), Some(want));
        }
        let ri = ReadIntent {
            obj: ObjRef::default(),
            version_id: 0,
            offset: 0,
            length: 0,
            size: 0,
            resident_bytes: 0,
            op: ReadOp::Read,
            policy: Policy::Wait,
            marker_flags: 0,
            quick_ms: 0,
            parked: false,
            identity: Identity::default(),
            principal: vec![],
            path_hint: vec![],
        };
        assert_eq!(
            encode(&Message::ReadIntent(ri), 0).len(),
            HEADER_LEN + 136 + 4
        );
    }

    #[test]
    fn known_layout_of_read_decision() {
        let d = ReadDecision {
            decision: Decision::Eio,
            reason: reason::RESTORE_FAILED,
            eta_s: 0x0102_0304,
            ttl_ms: 500,
            recall_id: 77,
            resident_bytes: 4096,
        };
        let rec = encode(&Message::ReadDecision(d), 0x8000_0000_0000_0005);
        assert_eq!(&rec[0..2], &[0x10, 0x80]);
        assert_eq!(&rec[4..8], &56u32.to_le_bytes());
        assert_eq!(rec[16], 3);
        assert_eq!(rec[17], 8);
        assert_eq!(&rec[20..24], &[4, 3, 2, 1]);
        assert_eq!(&rec[32..40], &77u64.to_le_bytes());
    }

    #[test]
    fn long_strings_are_truncated_and_flagged() {
        let mut path = vec![b'a'; 3000];
        path.extend_from_slice(b"/tail.segy");
        let ri = ReadIntent {
            obj: ObjRef::default(),
            version_id: 1,
            offset: 0,
            length: 1,
            size: 1,
            resident_bytes: 0,
            op: ReadOp::Read,
            policy: Policy::Wait,
            marker_flags: 0,
            quick_ms: 0,
            parked: false,
            identity: Identity::default(),
            principal: vec![b'p'; 5000],
            path_hint: path,
        };
        let rec = encode(&Message::ReadIntent(ri), 1);
        assert!(rec.len() <= MAX_MESSAGE);
        let f = decode(&rec).unwrap();
        assert_eq!(f.header.flags & FLAG_TRUNCATED, FLAG_TRUNCATED);
        let Message::ReadIntent(back) = f.msg else {
            panic!()
        };
        assert_eq!(back.path_hint.len(), MAX_PATH_HINT);
        assert!(
            back.path_hint.ends_with(b"/tail.segy"),
            "paths keep their tail"
        );
        assert_eq!(rec.len(), MAX_MESSAGE);
    }

    #[test]
    fn malformed_records() {
        assert!(matches!(
            decode(&[0u8; 5]),
            Err(DecodeError::ShortHeader(5))
        ));
        let mut rec = encode(&Message::Ping { mono_ns: 1 }, 1);
        rec[4] = 99;
        assert!(matches!(
            decode(&rec),
            Err(DecodeError::LengthMismatch { .. })
        ));
        // Fixed part shorter than the contract.
        let mut rec = encode(&Message::Ping { mono_ns: 1 }, 1);
        rec.truncate(20);
        rec[4..8].copy_from_slice(&20u32.to_le_bytes());
        assert!(matches!(decode(&rec), Err(DecodeError::ShortFixed { .. })));
        // String overrun.
        let mut rec = encode(
            &Message::Error {
                code: 3,
                message: b"bad".to_vec(),
            },
            0,
        );
        rec[20] = 200;
        assert!(matches!(decode(&rec), Err(DecodeError::BadString { .. })));
        // HELLO with a wrong magic.
        let mut rec = encode(
            &Message::Hello(Hello {
                proto_major: 0,
                proto_minor: 1,
                features: 0,
                pid: 1,
                instance: 1,
                ganesha_version: vec![],
                fsal_build: vec![],
            }),
            1,
        );
        rec[16] ^= 0xFF;
        assert!(decode(&rec).is_err());
    }

    #[test]
    fn newer_hello_with_longer_fixed_part_is_skipped_not_misparsed() {
        // A minor-2 HELLO: the 32-byte fixed part, 8 appended bytes, then
        // its strings. The extra bytes must not be read as a string.
        let mut rec = encode(
            &Message::Hello(Hello {
                proto_major: PROTO_MAJOR,
                proto_minor: PROTO_MINOR + 1,
                features: feature::PARK,
                pid: 7,
                instance: 9,
                ganesha_version: vec![],
                fsal_build: vec![],
            }),
            1,
        );
        // Drop the two empty strings, append 8 fixed-part bytes that would
        // decode as a string of length 0x0404, then the real strings.
        rec.truncate(HEADER_LEN + 32);
        rec.extend_from_slice(&[4, 4, 0, 0, 0, 0, 0, 0]);
        rec.extend_from_slice(&[5, 0]);
        rec.extend_from_slice(b"V16.0");
        rec.extend_from_slice(&[1, 0, b'x']);
        let len = rec.len() as u32;
        rec[4..8].copy_from_slice(&len.to_le_bytes());
        let f = decode(&rec).expect("a newer HELLO is accepted");
        let Message::Hello(h) = f.msg else { panic!() };
        assert_eq!((h.proto_minor, h.pid, h.instance), (PROTO_MINOR + 1, 7, 9));
        assert!(h.ganesha_version.is_empty() && h.fsal_build.is_empty());
    }

    #[test]
    fn hello_magic_bytes() {
        let rec = encode(
            &Message::Hello(Hello {
                proto_major: 0,
                proto_minor: 1,
                features: feature::PARK,
                pid: 1,
                instance: 2,
                ganesha_version: b"V15.6".to_vec(),
                fsal_build: b"abc".to_vec(),
            }),
            1,
        );
        assert_eq!(&rec[16..20], b"OMFS");
    }
}
