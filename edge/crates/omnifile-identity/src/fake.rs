//! In-memory fakes for tests here and in other crates (the smb-guard, the
//! daemon, `omnifile-acl`). `FakeIdMap::dev_ad()` holds the dev AD's real ids
//! and SIDs (`dev/README.md` seed data, read from omnifile-edge 2026-10-05;
//! `dev/down.sh --destroy` makes a new domain SID, which these do not track).

use std::collections::HashMap;
use std::os::fd::BorrowedFd;
use std::sync::atomic::{AtomicBool, AtomicUsize, Ordering};
use std::sync::{Arc, Mutex};

use crate::idmap::{IdMap, IdMapError, SidKind};
use crate::resolver::{Attribution, IdentityResolver, NfsRequest, ResolvedIdentity, SmbRequest};
use crate::sid::Sid;
use crate::smb::{ProcInfo, SessionSource, SmbSession};
use crate::trust::{NfsSec, Proof, Via};

#[derive(Clone, Debug, Default)]
pub struct FakeIdMap {
    /// name (lowercase, no domain) → (sid, kind, unix id)
    entries: Vec<(String, Sid, SidKind, u32)>,
    /// user sid → token sids
    tokens: HashMap<Sid, Vec<Sid>>,
    pub domain: String,
}

impl FakeIdMap {
    pub const DOMAIN_SID: &'static str = "S-1-5-21-3190051465-787089565-3716134449";
    pub const DEV_READER: &'static str = "S-1-5-21-3190051465-787089565-3716134449-1108";

    pub fn new(domain: &str) -> Self {
        FakeIdMap {
            domain: domain.to_string(),
            ..Default::default()
        }
    }

    pub fn add_user(&mut self, name: &str, uid: u32, sid: &str) -> &mut Self {
        self.entries.push((
            name.to_lowercase(),
            Sid::parse(sid).expect("sid"),
            SidKind::User,
            uid,
        ));
        self
    }

    pub fn add_group(&mut self, name: &str, gid: u32, sid: &str) -> &mut Self {
        self.entries.push((
            name.to_lowercase(),
            Sid::parse(sid).expect("sid"),
            SidKind::Group,
            gid,
        ));
        self
    }

    pub fn set_token(&mut self, user_sid: &str, groups: &[&str]) -> &mut Self {
        let u = Sid::parse(user_sid).expect("sid");
        let mut v = vec![u.clone()];
        v.extend(groups.iter().map(|g| Sid::parse(g).expect("sid")));
        self.tokens.insert(u, v);
        self
    }

    /// The seeded dev AD (`dev/README.md`), with its real SIDs.
    pub fn dev_ad() -> Self {
        let d = Self::DOMAIN_SID;
        let s = |rid: u32| format!("{d}-{rid}");
        let mut m = FakeIdMap::new("OMNI");
        m.add_user("geo-reader", 20001, &s(1108))
            .add_user("geo-writer", 20002, &s(1109))
            .add_user("geo-none", 20003, &s(1110))
            .add_user("omni-admin", 20004, &s(1111))
            .add_user("geo-owner", 20005, &s(1112))
            .add_user("geo-nested", 20006, &s(1113))
            .add_group("proj-readers", 30001, &s(1103))
            .add_group("proj-writers", 30002, &s(1104))
            .add_group("proj-owners", 30003, &s(1105))
            .add_group("omnifile-admins", 30004, &s(1106))
            .add_group("proj-readers-nested", 30005, &s(1107))
            .add_group("domain users", 10000, &s(513));
        let du = s(513);
        m.set_token(&s(1108), &[&du, &s(1103)])
            .set_token(&s(1109), &[&du, &s(1104)])
            .set_token(&s(1110), &[&du])
            .set_token(&s(1111), &[&du, &s(1106)])
            .set_token(&s(1112), &[&du, &s(1105)])
            .set_token(&s(1113), &[&du, &s(1107), &s(1103)]);
        m
    }

    fn by_id(&self, id: u32, user: bool) -> Option<&(String, Sid, SidKind, u32)> {
        self.entries
            .iter()
            .find(|e| e.3 == id && (e.2 == SidKind::User) == user)
    }

    fn by_sid(&self, sid: &Sid) -> Option<&(String, Sid, SidKind, u32)> {
        self.entries.iter().find(|e| &e.1 == sid)
    }
}

impl IdMap for FakeIdMap {
    fn uid_to_sid(&self, uid: u32) -> Result<Option<Sid>, IdMapError> {
        Ok(self.by_id(uid, true).map(|e| e.1.clone()))
    }
    fn gid_to_sid(&self, gid: u32) -> Result<Option<Sid>, IdMapError> {
        Ok(self.by_id(gid, false).map(|e| e.1.clone()))
    }
    fn sid_to_uid(&self, sid: &Sid) -> Result<Option<u32>, IdMapError> {
        Ok(self
            .by_sid(sid)
            .filter(|e| e.2 == SidKind::User)
            .map(|e| e.3))
    }
    fn sid_to_gid(&self, sid: &Sid) -> Result<Option<u32>, IdMapError> {
        Ok(self
            .by_sid(sid)
            .filter(|e| e.2 != SidKind::User)
            .map(|e| e.3))
    }
    fn name_to_sid(&self, name: &str) -> Result<Option<(Sid, SidKind)>, IdMapError> {
        let n = name.rsplit('\\').next().unwrap_or(name);
        let n = n.split('@').next().unwrap_or(n).to_lowercase();
        Ok(self
            .entries
            .iter()
            .find(|e| e.0 == n)
            .map(|e| (e.1.clone(), e.2)))
    }
    fn sid_to_name(&self, sid: &Sid) -> Result<Option<String>, IdMapError> {
        Ok(self
            .by_sid(sid)
            .map(|e| format!("{}\\{}", self.domain, e.0)))
    }
    fn user_token_sids(&self, user: &Sid) -> Result<Option<Vec<Sid>>, IdMapError> {
        Ok(self.tokens.get(user).cloned())
    }
}

/// A session table that tests can change and observe.
#[derive(Clone, Default)]
pub struct FakeSessions {
    list: Arc<Mutex<Vec<SmbSession>>>,
    calls: Arc<AtomicUsize>,
    fail: Arc<AtomicBool>,
    #[allow(clippy::type_complexity)]
    hook: Arc<Mutex<Option<Box<dyn Fn() + Send + Sync>>>>,
}

impl FakeSessions {
    /// Two sessions: pid 4242 = geo-reader, pid 5151 = geo-nested.
    pub fn fixture() -> Self {
        let mk = |id: &str, pid, uid, name: &str, host: &str| SmbSession {
            session_id: id.into(),
            pid,
            uid: Some(uid),
            gid: Some(10000),
            username: name.into(),
            groupname: "OMNI\\domain users".into(),
            remote_machine: String::new(),
            hostname: host.into(),
            dialect: "SMB3_11".into(),
        };
        let s = FakeSessions::default();
        s.push(mk(
            "2216454391",
            4242,
            20001,
            "OMNI\\geo-reader",
            "ipv4:192.168.104.7:50122",
        ));
        s.push(mk("77", 5151, 20006, "OMNI\\geo-nested", "ipv6:::1:41000"));
        s
    }

    pub fn push(&self, s: SmbSession) {
        self.list.lock().unwrap().push(s);
    }
    pub fn list(&self) -> Vec<SmbSession> {
        self.list.lock().unwrap().clone()
    }
    pub fn clear(&self) {
        self.list.lock().unwrap().clear();
    }
    pub fn calls(&self) -> usize {
        self.calls.load(Ordering::SeqCst)
    }
    pub fn fail(&self, f: bool) {
        self.fail.store(f, Ordering::SeqCst);
    }
    /// Runs `f` inside every `sessions()` call (to simulate races).
    pub fn on_call(&self, f: impl Fn() + Send + Sync + 'static) {
        *self.hook.lock().unwrap() = Some(Box::new(f));
    }
}

impl SessionSource for FakeSessions {
    fn sessions(&self) -> Result<Vec<SmbSession>, IdMapError> {
        self.calls.fetch_add(1, Ordering::SeqCst);
        if let Some(h) = self.hook.lock().unwrap().as_ref() {
            h();
        }
        if self.fail.load(Ordering::SeqCst) {
            return Err(IdMapError::Unavailable("smbstatus failed".into()));
        }
        Ok(self.list())
    }
}

/// (comm, start time, uid)
type ProcRow = (String, u64, u32);

/// Processes by pid.
#[derive(Clone, Default)]
pub struct FakeProc {
    procs: Arc<Mutex<HashMap<i32, ProcRow>>>,
    pidfd_dead: Arc<AtomicBool>,
    /// pid → fsuids of threads blocked on a fanotify answer. A pid absent
    /// here means "thread scan unavailable" (no CAP_SYS_PTRACE).
    waiting: Arc<Mutex<HashMap<i32, Vec<u32>>>>,
}

impl FakeProc {
    /// Creates (or replaces, i.e. reuses the pid of) a process.
    pub fn spawn(&self, pid: i32, comm: &str, start: u64) {
        self.procs
            .lock()
            .unwrap()
            .insert(pid, (comm.into(), start, 0));
    }
    pub fn set_uid(&self, pid: i32, uid: u32) {
        if let Some(p) = self.procs.lock().unwrap().get_mut(&pid) {
            p.2 = uid;
        }
    }
    pub fn exit(&self, pid: i32) {
        self.procs.lock().unwrap().remove(&pid);
    }
    /// The uids of the threads of `pid` blocked on the guard.
    pub fn set_waiting(&self, pid: i32, uids: &[u32]) {
        self.waiting.lock().unwrap().insert(pid, uids.to_vec());
    }
    /// What `pidfd_alive` answers.
    pub fn set_pidfd_dead(&self, dead: bool) {
        self.pidfd_dead.store(dead, Ordering::SeqCst);
    }
}

impl ProcInfo for FakeProc {
    fn start_time(&self, pid: i32) -> Option<u64> {
        self.procs.lock().unwrap().get(&pid).map(|p| p.1)
    }
    fn comm(&self, pid: i32) -> Option<String> {
        self.procs.lock().unwrap().get(&pid).map(|p| p.0.clone())
    }
    fn uid(&self, pid: i32) -> Option<u32> {
        self.procs.lock().unwrap().get(&pid).map(|p| p.2)
    }
    fn pidfd_alive(&self, _pidfd: BorrowedFd<'_>) -> bool {
        !self.pidfd_dead.load(Ordering::SeqCst)
    }
    fn waiting_fsuids(&self, pid: i32) -> Result<Vec<u32>, String> {
        self.waiting
            .lock()
            .unwrap()
            .get(&pid)
            .cloned()
            .ok_or_else(|| "fake: thread scan unavailable".to_string())
    }
}

/// A resolver with fixed answers: SMB pids and NFS uids map to users of
/// [`FakeIdMap::dev_ad`]. Unknown ones resolve to "unknown".
#[derive(Clone, Default)]
pub struct FakeResolver {
    pub idmap: FakeIdMap,
    /// smbd pid → uid
    pub smb: HashMap<i32, u32>,
}

impl FakeResolver {
    pub fn dev_ad() -> Self {
        FakeResolver {
            idmap: FakeIdMap::dev_ad(),
            smb: HashMap::new(),
        }
    }
}

impl IdentityResolver for FakeResolver {
    fn resolve_nfs(&self, req: &NfsRequest) -> ResolvedIdentity {
        let sid = req
            .uid
            .and_then(|u| self.idmap.uid_to_sid(u).ok().flatten());
        ResolvedIdentity {
            uid: req.uid,
            gid: req.gid,
            error: sid.is_none().then(|| "unknown".to_string()),
            sid,
            username: None,
            via: Via::Nfs,
            sec: req.sec.map(NfsSec::as_sec),
            proof: if req.sec.is_some_and(NfsSec::is_kerberos) {
                Proof::Proven
            } else {
                Proof::Claimed
            },
            client_addr: req.client_addr.clone(),
            process: None,
            method: if req.uid.is_some() {
                Attribution::NfsCredential
            } else {
                Attribution::Unknown
            },
        }
    }

    fn resolve_smb(&self, req: &SmbRequest<'_>) -> ResolvedIdentity {
        let uid = self.smb.get(&req.pid).copied();
        let sid = uid.and_then(|u| self.idmap.uid_to_sid(u).ok().flatten());
        ResolvedIdentity {
            uid,
            gid: None,
            error: sid.is_none().then(|| "unknown".to_string()),
            username: sid
                .as_ref()
                .and_then(|s| self.idmap.sid_to_name(s).ok().flatten()),
            proof: if sid.is_some() {
                Proof::Proven
            } else {
                Proof::Claimed
            },
            sid,
            via: Via::Smb,
            sec: None,
            client_addr: None,
            process: None,
            method: if uid.is_some() {
                Attribution::SessionTable
            } else {
                Attribution::Unknown
            },
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn dev_ad_matches_seed_table() {
        let m = FakeIdMap::dev_ad();
        let nested = m.name_to_sid("OMNI\\geo-nested").unwrap().unwrap().0;
        assert_eq!(m.sid_to_uid(&nested).unwrap(), Some(20006));
        let token = m.user_token_sids(&nested).unwrap().unwrap();
        let readers = m.gid_to_sid(30001).unwrap().unwrap();
        assert!(
            token.contains(&readers),
            "nested membership in Proj-Readers"
        );
        assert_eq!(
            m.sid_to_name(&readers).unwrap().as_deref(),
            Some("OMNI\\proj-readers")
        );
        assert_eq!(m.sid_to_gid(&readers).unwrap(), Some(30001));
        assert_eq!(m.sid_to_uid(&readers).unwrap(), None);
    }

    #[test]
    fn fake_resolver() {
        let mut r = FakeResolver::dev_ad();
        r.smb.insert(9, 20002);
        let id = r.resolve_smb(&SmbRequest {
            pid: 9,
            pidfd: None,
        });
        assert_eq!(id.username.as_deref(), Some("OMNI\\geo-writer"));
        assert_eq!(id.proof, Proof::Proven);
        let none = r.resolve_smb(&SmbRequest {
            pid: 10,
            pidfd: None,
        });
        assert!(none.sid.is_none());
    }
}
