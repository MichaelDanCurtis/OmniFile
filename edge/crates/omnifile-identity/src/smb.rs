//! SMB sessions: which AD user an smbd process is serving (spike A12).
//!
//! A fanotify event's pid is the tgid of the smbd child that serves one client
//! connection. `smbstatus --json` lists the sessions on that connection, with
//! the session user's uid and `DOMAIN\name`. The lookup must happen while the
//! event is still pending: once the client disconnects, the session is gone.

use std::time::Duration;

use serde_json::Value;

use crate::cmd::run_bounded;
use crate::idmap::IdMapError;

/// One SMB session, as `smbstatus --json` reports it.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct SmbSession {
    pub session_id: String,
    /// The smbd process serving it (`server_id.pid`).
    pub pid: i32,
    pub uid: Option<u32>,
    pub gid: Option<u32>,
    /// `DOMAIN\name` (empty for anonymous).
    pub username: String,
    pub groupname: String,
    /// `remote_machine`: the client's address or NetBIOS name.
    pub remote_machine: String,
    /// `hostname`: `ipv4:10.1.2.3:51234` / `ipv6:…`.
    pub hostname: String,
    pub dialect: String,
}

impl SmbSession {
    /// The client's IP address, from `hostname` (`ipv4:A.B.C.D:port`,
    /// `ipv6:…:port`), falling back to `remote_machine` when it parses.
    pub fn client_addr(&self) -> Option<String> {
        let h = self.hostname.as_str();
        let addr = if let Some(rest) = h.strip_prefix("ipv4:") {
            rest.rsplit_once(':').map(|(a, _)| a).unwrap_or(rest)
        } else if let Some(rest) = h.strip_prefix("ipv6:") {
            rest.rsplit_once(':').map(|(a, _)| a).unwrap_or(rest)
        } else {
            ""
        };
        let addr = addr.trim_start_matches('[').trim_end_matches(']');
        if addr.parse::<std::net::IpAddr>().is_ok() {
            return Some(addr.to_string());
        }
        self.remote_machine
            .parse::<std::net::IpAddr>()
            .ok()
            .map(|a| a.to_string())
    }

    /// Anonymous or guest sessions are not AD-authenticated. The product's
    /// smb.conf forbids them (`map to guest = never`, `restrict anonymous = 2`),
    /// so seeing one is itself worth an audit record.
    pub fn is_anonymous(&self) -> bool {
        let u = self
            .username
            .rsplit('\\')
            .next()
            .unwrap_or("")
            .to_lowercase();
        u.is_empty() || u == "nobody" || u == "guest" || self.uid == Some(65534)
    }
}

fn as_i64(v: &Value) -> Option<i64> {
    match v {
        Value::Number(n) => n.as_i64(),
        Value::String(s) => s.trim().parse().ok(),
        _ => None,
    }
}

fn as_str(v: Option<&Value>) -> String {
    match v {
        Some(Value::String(s)) => s.clone(),
        Some(Value::Number(n)) => n.to_string(),
        _ => String::new(),
    }
}

/// Parses the `sessions` object of `smbstatus --json` (Samba ≥ 4.16). Entries
/// without a usable pid are skipped. Unknown fields are ignored, so a newer
/// Samba that adds fields still parses.
pub fn parse_smbstatus_json(text: &str) -> Result<Vec<SmbSession>, IdMapError> {
    // smbstatus may print a warning line before the JSON when not run as root;
    // start at the first '{'.
    let start = text
        .find('{')
        .ok_or_else(|| IdMapError::BadOutput("smbstatus: no JSON".into()))?;
    let v: Value = serde_json::from_str(&text[start..])
        .map_err(|e| IdMapError::BadOutput(format!("smbstatus JSON: {e}")))?;
    let mut out = Vec::new();
    let Some(sessions) = v.get("sessions").and_then(Value::as_object) else {
        return Ok(out);
    };
    for (key, s) in sessions {
        let Some(pid) = s
            .get("server_id")
            .and_then(|sid| sid.get("pid"))
            .and_then(as_i64)
            .and_then(|p| i32::try_from(p).ok())
            .filter(|p| *p > 0)
        else {
            continue;
        };
        let id = |k: &str| {
            s.get(k)
                .and_then(as_i64)
                .and_then(|n| u32::try_from(n).ok())
                .filter(|n| *n != u32::MAX)
        };
        let session_id = match s.get("session_id") {
            Some(v) => as_str(Some(v)),
            None => key.clone(),
        };
        out.push(SmbSession {
            session_id,
            pid,
            uid: id("uid"),
            gid: id("gid"),
            username: as_str(s.get("username")),
            groupname: as_str(s.get("groupname")),
            remote_machine: as_str(s.get("remote_machine")),
            hostname: as_str(s.get("hostname")),
            dialect: as_str(s.get("session_dialect")),
        });
    }
    out.sort_by(|a, b| a.session_id.cmp(&b.session_id));
    Ok(out)
}

/// Where SMB sessions come from (real: `smbstatus`; tests: a fixture).
pub trait SessionSource: Send + Sync {
    fn sessions(&self) -> Result<Vec<SmbSession>, IdMapError>;
}

/// `smbstatus --processes --json`. Needs root (the daemon is root). It reads
/// Samba's session databases directly and does not talk to smbd, so it costs
/// a few milliseconds and does not depend on smbd being responsive.
#[derive(Clone, Debug)]
pub struct SmbstatusCmd {
    pub program: String,
    pub timeout: Duration,
}

impl Default for SmbstatusCmd {
    fn default() -> Self {
        SmbstatusCmd {
            program: "smbstatus".into(),
            timeout: Duration::from_secs(3),
        }
    }
}

impl SessionSource for SmbstatusCmd {
    fn sessions(&self) -> Result<Vec<SmbSession>, IdMapError> {
        let out = run_bounded(&self.program, &["--processes", "--json"], self.timeout)
            .map_err(|e| IdMapError::Unavailable(e.to_string()))?;
        parse_smbstatus_json(&out)
    }
}

/// What the resolver needs to know about a process. Real: `/proc` and
/// `pidfd_send_signal`; tests: a fake.
pub trait ProcInfo: Send + Sync {
    /// Start time in clock ticks since boot (`/proc/<pid>/stat` field 22).
    /// With the pid, it names one process for its whole life.
    fn start_time(&self, pid: i32) -> Option<u64>;
    /// `/proc/<pid>/comm`.
    fn comm(&self, pid: i32) -> Option<String>;
    /// Real uid of the process (`/proc/<pid>/status` `Uid:` first field).
    fn uid(&self, pid: i32) -> Option<u32>;
    /// Is the process this pidfd refers to still alive?
    fn pidfd_alive(&self, pidfd: std::os::fd::BorrowedFd<'_>) -> bool;
    /// The filesystem uids (`Uid:` fourth field) of the threads of `pid` that
    /// are blocked waiting for a fanotify permission answer (wait channel
    /// `fanotify_get_response`), duplicates removed, uid 0 included.
    ///
    /// smbd impersonates the session user on the thread that does the I/O,
    /// so the blocked thread's fsuid is the user of the request the guard is
    /// holding. Reading another process's `wchan` needs ptrace read access:
    /// `CAP_SYS_PTRACE` for the daemon. Without it the kernel reports `0`
    /// for every wchan, so this returns `Err` (never an empty list that
    /// would look like "nobody waiting").
    fn waiting_fsuids(&self, pid: i32) -> Result<Vec<u32>, String>;
}

/// The fourth (`fs`) field of `Uid:` in `/proc/…/status`.
pub(crate) fn parse_status_fsuid(status: &str) -> Option<u32> {
    status
        .lines()
        .find_map(|l| l.strip_prefix("Uid:"))?
        .split_whitespace()
        .nth(3)?
        .parse()
        .ok()
}

/// `CapEff` of `/proc/self/status` includes `CAP_SYS_PTRACE` (bit 19).
pub(crate) fn status_has_cap(status: &str, bit: u32) -> bool {
    status
        .lines()
        .find_map(|l| l.strip_prefix("CapEff:"))
        .and_then(|v| u64::from_str_radix(v.trim(), 16).ok())
        .is_some_and(|c| c & (1u64 << bit) != 0)
}

const CAP_SYS_PTRACE: u32 = 19;

/// `/proc` on Linux. On other systems every lookup returns `None`.
#[derive(Clone, Copy, Debug, Default)]
pub struct SysProc;

/// Field 22 of `/proc/<pid>/stat`. `comm` (field 2) may contain spaces and
/// parentheses, so fields are counted after the last `)`.
pub(crate) fn parse_stat_starttime(stat: &str) -> Option<u64> {
    let rest = &stat[stat.rfind(')')? + 1..];
    // rest starts with " S ppid …": field 3 is index 0 here; field 22 is index 19.
    rest.split_whitespace().nth(19)?.parse().ok()
}

pub(crate) fn parse_status_uid(status: &str) -> Option<u32> {
    status
        .lines()
        .find_map(|l| l.strip_prefix("Uid:"))?
        .split_whitespace()
        .next()?
        .parse()
        .ok()
}

impl ProcInfo for SysProc {
    fn start_time(&self, pid: i32) -> Option<u64> {
        let s = std::fs::read_to_string(format!("/proc/{pid}/stat")).ok()?;
        parse_stat_starttime(&s)
    }

    fn comm(&self, pid: i32) -> Option<String> {
        std::fs::read_to_string(format!("/proc/{pid}/comm"))
            .ok()
            .map(|s| s.trim_end().to_string())
    }

    fn uid(&self, pid: i32) -> Option<u32> {
        parse_status_uid(&std::fs::read_to_string(format!("/proc/{pid}/status")).ok()?)
    }

    #[cfg(target_os = "linux")]
    fn pidfd_alive(&self, pidfd: std::os::fd::BorrowedFd<'_>) -> bool {
        use std::os::fd::AsRawFd;
        // pidfd_send_signal(fd, 0, NULL, 0): 0 if the process exists, ESRCH
        // once it has exited (even if its pid number was reused since).
        // EPERM (a process of another uid, without CAP_KILL) also means it
        // exists: only ESRCH says it is gone, so no CAP_KILL is needed.
        // SAFETY: plain syscall with a borrowed, open fd and a null siginfo.
        let r = unsafe {
            libc::syscall(
                libc::SYS_pidfd_send_signal,
                pidfd.as_raw_fd(),
                0,
                std::ptr::null::<libc::siginfo_t>(),
                0,
            )
        };
        r == 0 || std::io::Error::last_os_error().raw_os_error() != Some(libc::ESRCH)
    }

    #[cfg(not(target_os = "linux"))]
    fn pidfd_alive(&self, _pidfd: std::os::fd::BorrowedFd<'_>) -> bool {
        false
    }

    fn waiting_fsuids(&self, pid: i32) -> Result<Vec<u32>, String> {
        static HAS_CAP: std::sync::OnceLock<bool> = std::sync::OnceLock::new();
        let has = *HAS_CAP.get_or_init(|| {
            let ok = std::fs::read_to_string("/proc/self/status")
                .is_ok_and(|s| status_has_cap(&s, CAP_SYS_PTRACE));
            if !ok {
                tracing::error!(
                    "no CAP_SYS_PTRACE: SMB reads on connections with several users \
                     cannot be attributed (thread scan unavailable); they are \
                     reported as unknown, and cached session answers as claimed"
                );
            }
            ok
        });
        if !has {
            return Err("thread scan unavailable: no CAP_SYS_PTRACE".into());
        }
        let dir = std::fs::read_dir(format!("/proc/{pid}/task"))
            .map_err(|e| format!("thread scan: /proc/{pid}/task: {e}"))?;
        let mut uids = std::collections::BTreeSet::new();
        let mut readable = false;
        for t in dir.flatten() {
            let tid = t.file_name().to_string_lossy().into_owned();
            let base = format!("/proc/{pid}/task/{tid}");
            let Ok(w) = std::fs::read_to_string(format!("{base}/wchan")) else {
                continue;
            };
            // "0" is what the kernel prints when ptrace access is refused
            // (or the task runs): only a symbol name proves we could read it.
            if w.is_empty() || w == "0" {
                continue;
            }
            readable = true;
            if w.starts_with("fanotify_get_response")
                && let Some(u) = std::fs::read_to_string(format!("{base}/status"))
                    .ok()
                    .as_deref()
                    .and_then(parse_status_fsuid)
            {
                uids.insert(u);
            }
        }
        if !readable {
            return Err(format!(
                "thread scan: no readable wchan in /proc/{pid}/task"
            ));
        }
        Ok(uids.into_iter().collect())
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    /// `smbstatus --processes --json` as captured on the matrix VM (Samba
    /// 4.23.6, `winbind use default domain = yes`, so names carry no domain),
    /// plus a second smbd and a session without a pid.
    pub(crate) const FIXTURE: &str = r#"smbstatus only works as root!
{"timestamp": "2026-10-05T13:27:50.124466-0700", "version": "4.23.6-Ubuntu-4.23.6+dfsg-1ubuntu2.2", "smb_conf": "/etc/samba/smb.conf",
 "sessions": {
  "3994358943": {"session_id": "3994358943",
    "server_id": {"pid": "4242", "task_id": "0", "vnn": "4294967295", "unique_id": "5563838430623818315"},
    "uid": 20001, "gid": 10000, "username": "geo-reader", "groupname": "domain users",
    "creation_time": "2026-10-05T13:27:49.300564-07:00", "expiration_time": "2026-10-05T23:26:58.300859+17:00",
    "auth_time": "2026-10-05T13:27:49.300564-07:00", "remote_machine": "192.168.104.7",
    "hostname": "ipv4:192.168.104.7:50122", "session_dialect": "SMB3_11",
    "client_guid": "5ffb705c-58ff-5d48-814a-9c59566e206e",
    "encryption": {"cipher": "-", "degree": "none"}, "signing": {"cipher": "AES-128-CMAC", "degree": "partial"},
    "channels": {"1": {"channel_id": "1", "creation_time": "2026-10-05T13:27:49.300564-07:00",
      "local_address": "ipv4:127.0.1.1:445", "remote_address": "ipv4:192.168.104.7:50122", "transport": "tcp"}}},
  "77": {"server_id": {"pid": 5151, "task_id": 0}, "uid": 20006, "gid": 10000,
    "username": "OMNI\\geo-nested", "groupname": "OMNI\\domain users",
    "remote_machine": "acl1", "hostname": "ipv6:::1:41000", "session_dialect": "SMB3_11"},
  "bad": {"uid": 1}
 }
}"#;

    #[test]
    fn parses_sessions() {
        let s = parse_smbstatus_json(FIXTURE).unwrap();
        assert_eq!(s.len(), 2);
        let r = s.iter().find(|x| x.pid == 4242).unwrap();
        assert_eq!(r.uid, Some(20001));
        assert_eq!(r.username, "geo-reader");
        assert_eq!(r.client_addr().as_deref(), Some("192.168.104.7"));
        assert!(!r.is_anonymous());
        let n = s.iter().find(|x| x.pid == 5151).unwrap();
        assert_eq!(n.session_id, "77");
        assert_eq!(n.client_addr().as_deref(), Some("::1"));
    }

    #[test]
    fn empty_and_broken_output() {
        assert!(
            parse_smbstatus_json(r#"{"timestamp":"x","version":"4.23"}"#)
                .unwrap()
                .is_empty()
        );
        assert!(parse_smbstatus_json("no json here").is_err());
        assert!(parse_smbstatus_json("{ broken").is_err());
    }

    #[test]
    fn anonymous_sessions() {
        let mut s = parse_smbstatus_json(FIXTURE).unwrap().remove(0);
        s.username = String::new();
        assert!(s.is_anonymous());
        s.username = "ACL1\\nobody".into();
        assert!(s.is_anonymous());
    }

    #[test]
    fn status_fields() {
        let st = "Name:\tsmbd\nUid:\t0\t20001\t0\t20001\nCapEff:\t000001ffffffffff\n";
        assert_eq!(parse_status_fsuid(st), Some(20001));
        assert!(status_has_cap(st, 19));
        assert!(!status_has_cap("CapEff:\t0000000000000000\n", 19));
        assert!(!status_has_cap("", 19));
    }

    #[test]
    fn proc_stat_fields() {
        let stat = "4242 (smbd[192.168.1) x) S 1 4242 4242 0 -1 4194624 1 0 0 0 0 0 0 0 20 0 1 0 987654 1 2 3";
        assert_eq!(parse_stat_starttime(stat), Some(987654));
        assert_eq!(parse_stat_starttime("garbage"), None);
        assert_eq!(
            parse_status_uid("Name:\tsmbd\nUid:\t20001\t20001\t0\t0\n"),
            Some(20001)
        );
    }

    #[cfg(target_os = "linux")]
    #[test]
    fn sysproc_reads_self() {
        let me = std::process::id() as i32;
        assert!(SysProc.start_time(me).is_some());
        assert!(SysProc.comm(me).is_some());
        assert_eq!(SysProc.uid(me), Some(unsafe { libc::getuid() }));
    }
}
