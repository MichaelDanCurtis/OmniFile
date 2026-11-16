//! The interface the daemon (NFS, via the FSAL socket) and the smb-guard
//! (fanotify events) call to learn who asked.

use std::os::fd::BorrowedFd;
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant};

use tracing::debug;

use crate::cache::TtlCache;
use crate::idmap::{CachedIdMap, IdMap, Wbinfo};
use crate::sid::Sid;
use crate::smb::{ProcInfo, SessionSource, SmbSession, SmbstatusCmd, SysProc};
use crate::trust::{NfsSec, Proof, Requester, Sec, Via};

/// An NFS caller, as `FSAL_OMNI` reports it (`fsal-socket.md` §4.2).
#[derive(Clone, Debug, Default, PartialEq, Eq)]
pub struct NfsRequest {
    /// `None` (or `0xFFFFFFFF` on the wire) when unknown.
    pub uid: Option<u32>,
    pub gid: Option<u32>,
    pub sec: Option<NfsSec>,
    pub client_addr: Option<String>,
}

/// A fanotify event's origin, as the smb-guard sees it.
#[derive(Clone, Copy, Debug)]
pub struct SmbRequest<'a> {
    /// The event pid: the tgid of the process that read (an smbd child for SMB).
    pub pid: i32,
    /// The event's pidfd (`FAN_REPORT_PIDFD`), when the kernel supplied one.
    /// The caller keeps it open until `resolve_smb` returns.
    pub pidfd: Option<BorrowedFd<'a>>,
}

/// How an identity was decided. Recorded so audit and recall records say why
/// a requester is `proven` or only `claimed`.
#[derive(Clone, Copy, Debug, PartialEq, Eq, Hash)]
pub enum Attribution {
    /// NFS: the RPC credential (`sec=sys` claimed, `krb5*` proven).
    NfsCredential,
    /// A process on the edge itself: its uid, from the kernel. Proven.
    LocalProcess,
    /// The only user on the smbd connection, from a session table read
    /// during this lookup (after the event was raised). Proven.
    SessionTable,
    /// The fsuid of the smbd thread blocked on this event (smbd impersonates
    /// the session user on the I/O thread), matched to a session on the same
    /// connection. Exact even when one smbd serves several users. Proven.
    ThreadAndSession,
    /// A session table older than this lookup (cached; rate-limited) named a
    /// single user, and no thread scan confirmed it. Claimed: a session added
    /// since could be the reader.
    SessionCache,
    /// The blocked thread's fsuid with no session on the connection to match
    /// it. Claimed.
    ThreadOnly,
    /// Nobody could be named (`uid`/`sid` absent; report uid `0xFFFFFFFF`).
    Unknown,
}

/// Who asked, as precisely as the edge can say.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct ResolvedIdentity {
    pub uid: Option<u32>,
    pub gid: Option<u32>,
    pub sid: Option<Sid>,
    /// `DOMAIN\name` when known.
    pub username: Option<String>,
    pub via: Via,
    pub sec: Option<Sec>,
    pub proof: Proof,
    pub client_addr: Option<String>,
    /// For [`Via::Local`]: the process name.
    pub process: Option<String>,
    /// Why the identity is incomplete, if it is. Never stops a read.
    pub error: Option<String>,
    /// How it was decided ([`Attribution`]).
    pub method: Attribution,
}

impl ResolvedIdentity {
    fn unknown(via: Via, error: impl Into<String>) -> Self {
        ResolvedIdentity {
            uid: None,
            gid: None,
            sid: None,
            username: None,
            via,
            sec: None,
            proof: Proof::Claimed,
            client_addr: None,
            process: None,
            error: Some(error.into()),
            method: Attribution::Unknown,
        }
    }

    /// The uid for the wire: `0xFFFFFFFF` when unknown, as in the FSAL
    /// contract's identity record (never 0, which names root).
    pub fn wire_uid(&self) -> u32 {
        self.uid.unwrap_or(u32::MAX)
    }

    /// The control API's `requester` (recalls §4.4, audit §4.7).
    pub fn requester(&self) -> Requester {
        Requester {
            uid: self.uid,
            sid: self.sid.clone(),
            via: self.via,
            sec: self.sec,
            proof: self.proof,
            client_addr: self.client_addr.clone(),
        }
    }
}

/// What the smb-guard and the daemon call. Implementations never fail and
/// bound their time (subprocess timeouts); call from a blocking thread, while
/// the request is still pending.
pub trait IdentityResolver: Send + Sync {
    fn resolve_nfs(&self, req: &NfsRequest) -> ResolvedIdentity;
    fn resolve_smb(&self, req: &SmbRequest<'_>) -> ResolvedIdentity;
}

impl<T: IdentityResolver + ?Sized> IdentityResolver for Arc<T> {
    fn resolve_nfs(&self, req: &NfsRequest) -> ResolvedIdentity {
        (**self).resolve_nfs(req)
    }
    fn resolve_smb(&self, req: &SmbRequest<'_>) -> ResolvedIdentity {
        (**self).resolve_smb(req)
    }
}

#[derive(Clone, Debug)]
pub struct ResolverConfig {
    /// How long a resolved (pid, start time) → session answer is reused.
    pub session_ttl: Duration,
    /// The minimum age of the session table before a miss re-reads it, so a
    /// flood of events from new connections costs one `smbstatus` per period.
    pub min_refresh: Duration,
    /// id-map cache lifetimes (answers, "no mapping").
    pub idmap_ttl: Duration,
    pub idmap_negative_ttl: Duration,
}

impl Default for ResolverConfig {
    fn default() -> Self {
        ResolverConfig {
            session_ttl: Duration::from_secs(5),
            min_refresh: Duration::from_millis(200),
            idmap_ttl: Duration::from_secs(300),
            idmap_negative_ttl: Duration::from_secs(30),
        }
    }
}

type SessionTable = Arc<Vec<SmbSession>>;

/// The production resolver: winbind for ids, `smbstatus` for sessions,
/// `/proc` and pidfds for processes.
pub struct Resolver<M, S, P> {
    idmap: M,
    sessions: S,
    procs: P,
    cfg: ResolverConfig,
    by_proc: TtlCache<(i32, u64), Vec<SmbSession>>,
    table: Mutex<Option<(Instant, SessionTable)>>,
}

impl Resolver<CachedIdMap<Wbinfo>, SmbstatusCmd, SysProc> {
    /// winbind via `wbinfo`, sessions via `smbstatus`, `/proc`.
    pub fn system(cfg: ResolverConfig) -> Self {
        let idmap = CachedIdMap::new(Wbinfo::default(), cfg.idmap_ttl, cfg.idmap_negative_ttl);
        Resolver::new(idmap, SmbstatusCmd::default(), SysProc, cfg)
    }
}

impl<M: IdMap, S: SessionSource, P: ProcInfo> Resolver<M, S, P> {
    pub fn new(idmap: M, sessions: S, procs: P, cfg: ResolverConfig) -> Self {
        Resolver {
            idmap,
            sessions,
            procs,
            by_proc: TtlCache::new(cfg.session_ttl, 65_536),
            table: Mutex::new(None),
            cfg,
        }
    }

    pub fn idmap(&self) -> &M {
        &self.idmap
    }

    /// The session table and when it was read. Re-read when `force` (or
    /// older than `session_ttl`), but never more often than `min_refresh`.
    fn table(&self, force: bool) -> Result<(SessionTable, Instant), String> {
        let mut t = self.table.lock().unwrap_or_else(|e| e.into_inner());
        if let Some((at, tab)) = t.as_ref() {
            let age = at.elapsed();
            if age < self.cfg.min_refresh || (!force && age < self.cfg.session_ttl) {
                return Ok((tab.clone(), *at));
            }
        }
        let tab = Arc::new(self.sessions.sessions().map_err(|e| e.to_string())?);
        let now = Instant::now();
        *t = Some((now, tab.clone()));
        Ok((tab, now))
    }

    /// This pid's sessions, and whether they come from a table read at or
    /// after `since`.
    fn sessions_of(
        &self,
        pid: i32,
        force: bool,
        since: Instant,
    ) -> Result<(Vec<SmbSession>, bool), String> {
        let (tab, at) = self.table(force)?;
        let found: Vec<SmbSession> = tab.iter().filter(|s| s.pid == pid).cloned().collect();
        if found.is_empty() && !force {
            // A connection newer than the cached table: re-read (rate-limited).
            return self.sessions_of(pid, true, since);
        }
        Ok((found, at >= since))
    }

    fn identity_of_session(
        &self,
        s: &SmbSession,
        proof: Proof,
        method: Attribution,
        note: Option<String>,
    ) -> ResolvedIdentity {
        if s.is_anonymous() {
            let mut r = ResolvedIdentity::unknown(Via::Smb, "anonymous or guest SMB session");
            r.uid = s.uid;
            r.client_addr = s.client_addr();
            return r;
        }
        let (sid, error) = match s.uid {
            Some(uid) => self.sid_of_uid(uid, Some(&s.username)),
            None => match self.idmap.name_to_sid(&s.username) {
                Ok(Some((sid, _))) => (Some(sid), None),
                Ok(None) => (None, Some(format!("{} has no SID", s.username))),
                Err(e) => (None, Some(e.to_string())),
            },
        };
        let error = match (error, note) {
            (Some(e), Some(n)) => Some(format!("{e}; {n}")),
            (e, n) => e.or(n),
        };
        ResolvedIdentity {
            uid: s.uid,
            gid: s.gid,
            sid,
            username: Some(s.username.clone()),
            via: Via::Smb,
            sec: None,
            proof,
            client_addr: s.client_addr(),
            process: None,
            error,
            method,
        }
    }

    fn sid_of_uid(&self, uid: u32, name: Option<&str>) -> (Option<Sid>, Option<String>) {
        match self.idmap.uid_to_sid(uid) {
            Ok(Some(s)) => return (Some(s), None),
            Ok(None) => {}
            Err(e) => return (None, Some(e.to_string())),
        }
        if let Some(n) = name.filter(|n| !n.is_empty()) {
            match self.idmap.name_to_sid(n) {
                Ok(Some((s, _))) => return (Some(s), None),
                Ok(None) => {}
                Err(e) => return (None, Some(e.to_string())),
            }
        }
        (None, Some(format!("uid {uid} has no AD SID")))
    }

    fn resolve_local(&self, pid: i32, comm: String) -> ResolvedIdentity {
        let Some(uid) = self.procs.uid(pid) else {
            return ResolvedIdentity::unknown(Via::Local, "process exited");
        };
        let (sid, error) = self.sid_of_uid(uid, None);
        // A local uid is known to the kernel, so it is proven; local accounts
        // such as root have no AD SID, which `error` records.
        ResolvedIdentity {
            uid: Some(uid),
            gid: None,
            sid,
            username: None,
            via: Via::Local,
            sec: None,
            proof: Proof::Proven,
            client_addr: None,
            process: Some(comm),
            error,
            method: Attribution::LocalProcess,
        }
    }
}

impl<M: IdMap, S: SessionSource, P: ProcInfo> IdentityResolver for Resolver<M, S, P> {
    fn resolve_nfs(&self, req: &NfsRequest) -> ResolvedIdentity {
        let uid = req.uid.filter(|u| *u != u32::MAX);
        let proof = match req.sec {
            Some(s) if s.is_kerberos() => Proof::Proven,
            _ => Proof::Claimed,
        };
        let (sid, error) = match uid {
            Some(u) => self.sid_of_uid(u, None),
            None => (None, Some("uid unknown".into())),
        };
        ResolvedIdentity {
            uid,
            gid: req.gid.filter(|g| *g != u32::MAX),
            sid,
            username: None,
            via: Via::Nfs,
            sec: req.sec.map(NfsSec::as_sec),
            proof,
            client_addr: req.client_addr.clone(),
            process: None,
            error,
            method: if uid.is_some() {
                Attribution::NfsCredential
            } else {
                Attribution::Unknown
            },
        }
    }

    fn resolve_smb(&self, req: &SmbRequest<'_>) -> ResolvedIdentity {
        let t0 = Instant::now();
        let pid = req.pid;
        let Some(start) = self.procs.start_time(pid) else {
            return ResolvedIdentity::unknown(Via::Smb, "process exited before lookup");
        };
        let comm = self.procs.comm(pid).unwrap_or_default();
        if !comm.starts_with("smbd") {
            return self.resolve_local(pid, comm);
        }
        let r = self.resolve_smbd(pid, start, t0);
        // The pid must still name the same process now that the lookup is
        // done; otherwise what we found may belong to a newer process that
        // reused the pid. With a pidfd this is exact.
        let same = match req.pidfd {
            Some(fd) => self.procs.pidfd_alive(fd),
            None => self.procs.start_time(pid) == Some(start),
        };
        if !same {
            debug!(pid, "smbd exited during identity lookup");
            self.by_proc.remove(&(pid, start));
            return ResolvedIdentity::unknown(Via::Smb, "process exited during lookup");
        }
        r
    }
}

impl<M: IdMap, S: SessionSource, P: ProcInfo> Resolver<M, S, P> {
    /// SMB attribution (spike A12, M2 review S5):
    ///
    /// 1. **Thread and session.** The fsuid of the smbd thread blocked on the
    ///    event is the user of the request, even when one smbd serves several
    ///    users (a Linux CIFS client multiplexes its users' sessions on one
    ///    connection). When exactly one uid is waiting in that smbd and a
    ///    session on the connection has that uid, that session is the
    ///    requester: proven. A root (uid 0) waiter is never skipped: with it
    ///    the waiting set is ambiguous and this step does not decide.
    /// 2. Otherwise **a single-user connection**: proven only when the session
    ///    table was read during this lookup; from an older table, claimed.
    /// 3. Otherwise **the waiting uid alone** (no matching session): claimed.
    /// 4. Otherwise unknown (uid `0xFFFFFFFF`, claimed).
    ///
    /// The per-process cache holds a connection's sessions only while it has
    /// one user, and is dropped as soon as a thread disagrees with it.
    fn resolve_smbd(&self, pid: i32, start: u64, t0: Instant) -> ResolvedIdentity {
        let scan = self.procs.waiting_fsuids(pid);
        let waiting_one = match &scan {
            Ok(v) if v.len() == 1 && v[0] != 0 => Some(v[0]),
            _ => None,
        };
        let key = (pid, start);
        let cached = self.by_proc.get(&key);
        let mut fresh = false;
        let mut sessions = match cached {
            Some(s) => s,
            None => match self.sessions_of(pid, false, t0) {
                Ok((s, f)) => {
                    fresh = f;
                    s
                }
                Err(e) => return self.thread_only(waiting_one, e),
            },
        };

        // 1. Thread and session.
        if let Some(u) = waiting_one {
            if !sessions.iter().any(|s| s.uid == Some(u)) && !fresh {
                // The cache or an old table predates the reader's session.
                self.by_proc.remove(&key);
                match self.sessions_of(pid, true, t0) {
                    Ok((s, _)) => sessions = s,
                    Err(e) => return self.thread_only(waiting_one, e),
                }
            }
            let matching: Vec<&SmbSession> = sessions.iter().filter(|s| s.uid == Some(u)).collect();
            if let Some(s) = matching.first() {
                let s = (*s).clone();
                self.remember(key, &sessions);
                return self.identity_of_session(
                    &s,
                    Proof::Proven,
                    Attribution::ThreadAndSession,
                    None,
                );
            }
            // The kernel says uid `u` is waiting and no session on this
            // connection has it, even in a fresh table: trust neither the
            // table's user nor the thread for proof.
            self.by_proc.remove(&key);
            return self.thread_only(
                waiting_one,
                format!("waiting uid {u} has no session on smbd pid {pid}"),
            );
        }

        // 2. A single-user connection.
        if sessions.is_empty() {
            return self.thread_only(waiting_one, format!("no SMB session for pid {pid}"));
        }
        let users = distinct_users(&sessions);
        if users == 1 {
            if !fresh {
                // Decide only from a table read after the event was raised.
                match self.sessions_of(pid, true, t0) {
                    Ok((s, f)) if !s.is_empty() => {
                        sessions = s;
                        fresh = f;
                    }
                    Ok(_) => {}
                    Err(e) => debug!(pid, err = %e, "session refresh failed"),
                }
            }
            if distinct_users(&sessions) == 1 {
                self.remember(key, &sessions);
                let s = sessions[0].clone();
                let note = scan.as_ref().err().map(|e| e.to_string());
                return if fresh {
                    self.identity_of_session(&s, Proof::Proven, Attribution::SessionTable, note)
                } else {
                    self.identity_of_session(&s, Proof::Claimed, Attribution::SessionCache, note)
                };
            }
        }

        // Several users on one connection, and the thread scan did not decide.
        self.by_proc.remove(&key);
        let why = format!(
            "{} users share smbd pid {pid}; thread scan: {}",
            distinct_users(&sessions),
            match &scan {
                Ok(v) => format!("waiting uids {v:?}"),
                Err(e) => e.clone(),
            }
        );
        let mut r = self.thread_only(waiting_one, why);
        if r.client_addr.is_none() {
            r.client_addr = sessions[0].client_addr();
        }
        r
    }

    fn remember(&self, key: (i32, u64), sessions: &[SmbSession]) {
        if distinct_users(sessions) == 1 {
            self.by_proc.put(key, sessions.to_vec());
        } else {
            self.by_proc.remove(&key);
        }
    }

    /// Step 3/4: the waiting thread's uid with no session to match, or nobody.
    fn thread_only(&self, waiting: Option<u32>, why: String) -> ResolvedIdentity {
        let Some(u) = waiting else {
            return ResolvedIdentity::unknown(Via::Smb, why);
        };
        let (sid, e) = self.sid_of_uid(u, None);
        ResolvedIdentity {
            uid: Some(u),
            gid: None,
            sid,
            username: None,
            via: Via::Smb,
            sec: None,
            proof: Proof::Claimed,
            client_addr: None,
            process: None,
            error: Some(match e {
                Some(e) => format!("{why}; {e}"),
                None => why,
            }),
            method: Attribution::ThreadOnly,
        }
    }
}

fn distinct_users(sessions: &[SmbSession]) -> usize {
    sessions
        .iter()
        .map(|x| (x.uid, x.username.to_lowercase()))
        .collect::<std::collections::BTreeSet<_>>()
        .len()
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::fake::{FakeIdMap, FakeProc, FakeSessions};

    fn resolver(
        sessions: FakeSessions,
        procs: FakeProc,
    ) -> Resolver<FakeIdMap, FakeSessions, FakeProc> {
        Resolver::new(
            FakeIdMap::dev_ad(),
            sessions,
            procs,
            ResolverConfig {
                min_refresh: Duration::ZERO,
                ..Default::default()
            },
        )
    }

    #[test]
    fn nfs_sys_is_claimed_krb5_is_proven() {
        let r = resolver(FakeSessions::default(), FakeProc::default());
        let sys = r.resolve_nfs(&NfsRequest {
            uid: Some(20001),
            gid: Some(10000),
            sec: Some(NfsSec::Sys),
            client_addr: Some("10.1.2.3".into()),
        });
        assert_eq!(sys.proof, Proof::Claimed);
        assert_eq!(sys.sid.as_ref().unwrap().as_str(), FakeIdMap::DEV_READER);
        assert_eq!(
            serde_json::to_value(sys.requester()).unwrap(),
            serde_json::json!({"uid":20001,"sid":FakeIdMap::DEV_READER,"via":"nfs","sec":"sys","proof":"claimed","client_addr":"10.1.2.3"})
        );
        let krb = r.resolve_nfs(&NfsRequest {
            uid: Some(20006),
            sec: Some(NfsSec::Krb5p),
            ..Default::default()
        });
        assert_eq!(krb.proof, Proof::Proven);
        assert_eq!(krb.sec, Some(Sec::Krb5p));
        let anon = r.resolve_nfs(&NfsRequest {
            uid: Some(u32::MAX),
            ..Default::default()
        });
        assert_eq!(anon.uid, None);
        assert!(anon.sid.is_none() && anon.error.is_some());
        let unmapped = r.resolve_nfs(&NfsRequest {
            uid: Some(0),
            sec: Some(NfsSec::Sys),
            ..Default::default()
        });
        assert!(unmapped.sid.is_none());
        assert_eq!(unmapped.uid, Some(0));
    }

    #[test]
    fn smb_pid_maps_to_session_user() {
        let procs = FakeProc::default();
        procs.spawn(4242, "smbd[192.168.1", 0);
        procs.set_waiting(4242, &[20001]);
        let r = resolver(FakeSessions::fixture(), procs);
        let id = r.resolve_smb(&SmbRequest {
            pid: 4242,
            pidfd: None,
        });
        assert_eq!(id.via, Via::Smb);
        assert_eq!(id.proof, Proof::Proven);
        assert_eq!(id.uid, Some(20001));
        assert_eq!(id.sid.as_ref().unwrap().as_str(), FakeIdMap::DEV_READER);
        assert_eq!(id.username.as_deref(), Some("OMNI\\geo-reader"));
        assert_eq!(id.client_addr.as_deref(), Some("192.168.104.7"));
        assert_eq!(id.method, Attribution::ThreadAndSession);
        assert!(id.error.is_none(), "{:?}", id.error);
    }

    /// A resolver whose session table may be reused for 60 s.
    fn slow_resolver(
        sessions: FakeSessions,
        procs: FakeProc,
    ) -> Resolver<FakeIdMap, FakeSessions, FakeProc> {
        Resolver::new(
            FakeIdMap::dev_ad(),
            sessions,
            procs,
            ResolverConfig {
                min_refresh: Duration::from_secs(60),
                session_ttl: Duration::from_secs(60),
                ..Default::default()
            },
        )
    }

    fn smb(r: &impl IdentityResolver, pid: i32) -> ResolvedIdentity {
        r.resolve_smb(&SmbRequest { pid, pidfd: None })
    }

    #[test]
    fn thread_scan_confirms_cached_sessions() {
        let procs = FakeProc::default();
        procs.spawn(4242, "smbd", 0);
        procs.set_waiting(4242, &[20001]);
        let sessions = FakeSessions::fixture();
        let r = slow_resolver(sessions.clone(), procs);
        for _ in 0..10 {
            let id = smb(&r, 4242);
            assert_eq!(id.uid, Some(20001));
            assert_eq!(id.proof, Proof::Proven);
            assert_eq!(id.method, Attribution::ThreadAndSession);
        }
        assert_eq!(sessions.calls(), 1);
    }

    #[test]
    fn cache_alone_is_only_claimed() {
        // No CAP_SYS_PTRACE (no thread scan) and a rate-limited table.
        let procs = FakeProc::default();
        procs.spawn(4242, "smbd", 0);
        let sessions = FakeSessions::fixture();
        let r = slow_resolver(sessions.clone(), procs);
        let first = smb(&r, 4242);
        assert_eq!(
            (first.proof, first.method),
            (Proof::Proven, Attribution::SessionTable)
        );
        assert!(
            first.error.unwrap().contains("thread scan"),
            "degradation is visible"
        );
        let again = smb(&r, 4242);
        assert_eq!(again.uid, Some(20001));
        assert_eq!(
            (again.proof, again.method),
            (Proof::Claimed, Attribution::SessionCache)
        );
        // With min_refresh 0 every answer comes from a fresh read: proven.
        let procs = FakeProc::default();
        procs.spawn(4242, "smbd", 0);
        let r = resolver(FakeSessions::fixture(), procs);
        smb(&r, 4242);
        assert_eq!(smb(&r, 4242).method, Attribution::SessionTable);
    }

    #[test]
    fn multiplexed_session_added_after_caching_is_attributed_exactly() {
        let procs = FakeProc::default();
        procs.spawn(4242, "smbd", 0);
        procs.set_waiting(4242, &[20001]);
        let sessions = FakeSessions::fixture();
        let r = Resolver::new(
            FakeIdMap::dev_ad(),
            sessions.clone(),
            procs.clone(),
            ResolverConfig {
                min_refresh: Duration::ZERO,
                session_ttl: Duration::from_secs(60),
                ..Default::default()
            },
        );
        assert_eq!(smb(&r, 4242).uid, Some(20001)); // cached: geo-reader only
        // A Linux CIFS client adds geo-writer's session on the same connection.
        let mut w = sessions.list()[0].clone();
        w.session_id = "9".into();
        w.uid = Some(20002);
        w.username = "geo-writer".into();
        sessions.push(w);
        procs.set_waiting(4242, &[20002]);
        let id = smb(&r, 4242);
        assert_eq!(id.uid, Some(20002), "not the cached first user");
        assert_eq!(id.username.as_deref(), Some("geo-writer"));
        assert_eq!(
            (id.proof, id.method),
            (Proof::Proven, Attribution::ThreadAndSession)
        );
        // Both users now: the cache is not used to pick one.
        procs.set_waiting(4242, &[20001]);
        assert_eq!(smb(&r, 4242).uid, Some(20001));
    }

    #[test]
    fn root_waiter_never_hands_the_event_to_another_user() {
        let procs = FakeProc::default();
        procs.spawn(4242, "smbd", 0);
        let sessions = FakeSessions::fixture();
        let mut w = sessions.list()[0].clone();
        w.session_id = "9".into();
        w.uid = Some(20002);
        w.username = "geo-writer".into();
        sessions.push(w);
        let r = resolver(sessions, procs.clone());
        // A root-euid smbd thread holds this event while geo-writer's waits.
        procs.set_waiting(4242, &[0, 20002]);
        let id = smb(&r, 4242);
        assert_eq!(id.uid, None);
        assert_eq!(id.wire_uid(), u32::MAX);
        assert_eq!(
            (id.proof, id.method),
            (Proof::Claimed, Attribution::Unknown)
        );
        procs.set_waiting(4242, &[0]);
        assert_eq!(smb(&r, 4242).method, Attribution::Unknown);
    }

    #[test]
    fn root_waiter_on_single_user_connection_uses_fresh_table() {
        let procs = FakeProc::default();
        procs.spawn(4242, "smbd", 0);
        procs.set_waiting(4242, &[0]);
        let r = resolver(FakeSessions::fixture(), procs);
        let id = smb(&r, 4242);
        assert_eq!(id.uid, Some(20001));
        assert_eq!(id.method, Attribution::SessionTable);
        assert_eq!(id.proof, Proof::Proven);
    }

    #[test]
    fn waiting_uid_without_session_is_claimed() {
        let procs = FakeProc::default();
        procs.spawn(4242, "smbd", 0);
        procs.set_waiting(4242, &[20003]);
        let r = resolver(FakeSessions::fixture(), procs);
        let id = smb(&r, 4242);
        assert_eq!(id.uid, Some(20003));
        assert!(id.sid.is_some());
        assert_eq!(
            (id.proof, id.method),
            (Proof::Claimed, Attribution::ThreadOnly)
        );
    }

    #[test]
    fn pid_reuse_during_lookup_is_never_attributed() {
        let procs = FakeProc::default();
        procs.spawn(4242, "smbd", 0);
        let sessions = FakeSessions::fixture();
        // The process exits and the pid is reused while smbstatus runs.
        let p2 = procs.clone();
        sessions.on_call(move || p2.spawn(4242, "smbd", 1));
        let r = resolver(sessions, procs);
        let id = r.resolve_smb(&SmbRequest {
            pid: 4242,
            pidfd: None,
        });
        assert!(id.sid.is_none());
        assert_eq!(id.error.as_deref(), Some("process exited during lookup"));
    }

    #[test]
    fn exited_process_and_missing_session() {
        let procs = FakeProc::default();
        procs.spawn(6000, "smbd", 0);
        let r = resolver(FakeSessions::fixture(), procs);
        let gone = r.resolve_smb(&SmbRequest {
            pid: 1,
            pidfd: None,
        });
        assert_eq!(gone.error.as_deref(), Some("process exited before lookup"));
        let none = r.resolve_smb(&SmbRequest {
            pid: 6000,
            pidfd: None,
        });
        assert!(none.error.unwrap().contains("no SMB session"));
    }

    #[test]
    fn several_users_on_one_connection_are_not_guessed() {
        let procs = FakeProc::default();
        procs.spawn(4242, "smbd", 0);
        let sessions = FakeSessions::fixture();
        let mut extra = sessions.list()[0].clone();
        extra.session_id = "9".into();
        extra.uid = Some(20002);
        extra.username = "OMNI\\geo-writer".into();
        sessions.push(extra);
        let r = resolver(sessions, procs);
        let id = r.resolve_smb(&SmbRequest {
            pid: 4242,
            pidfd: None,
        });
        assert!(id.sid.is_none());
        assert_eq!(id.proof, Proof::Claimed);
        assert_eq!(id.wire_uid(), u32::MAX);
        assert!(id.error.unwrap().contains("2 users"));
    }

    #[test]
    fn local_process_is_reported_as_local() {
        let procs = FakeProc::default();
        procs.spawn(777, "cat", 0);
        procs.set_uid(777, 20002);
        let r = resolver(FakeSessions::fixture(), procs);
        let id = r.resolve_smb(&SmbRequest {
            pid: 777,
            pidfd: None,
        });
        assert_eq!(id.via, Via::Local);
        assert_eq!(id.uid, Some(20002));
        assert_eq!(id.process.as_deref(), Some("cat"));
        assert_eq!(id.proof, Proof::Proven);
        assert!(id.sid.is_some());
    }

    #[test]
    fn session_source_failure_is_reported_not_fatal() {
        let procs = FakeProc::default();
        procs.spawn(4242, "smbd", 0);
        let sessions = FakeSessions::fixture();
        sessions.fail(true);
        let r = resolver(sessions, procs);
        let id = r.resolve_smb(&SmbRequest {
            pid: 4242,
            pidfd: None,
        });
        assert!(id.sid.is_none() && id.error.is_some());
    }

    #[cfg(target_os = "linux")]
    #[test]
    fn real_pidfd_liveness() {
        use std::os::fd::{AsFd, FromRawFd, OwnedFd};
        let mut child = std::process::Command::new("sleep")
            .arg("5")
            .spawn()
            .unwrap();
        let pid = child.id() as i32;
        // SAFETY: pidfd_open on a child we own.
        let fd = unsafe { libc::syscall(libc::SYS_pidfd_open, pid, 0) } as i32;
        assert!(fd >= 0);
        let fd = unsafe { OwnedFd::from_raw_fd(fd) };
        assert!(SysProc.pidfd_alive(fd.as_fd()));
        child.kill().unwrap();
        child.wait().unwrap();
        assert!(!SysProc.pidfd_alive(fd.as_fd()));
    }
}
