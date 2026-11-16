//! omnifile-guard: throwaway spike of the fanotify pre-content stub guard.
//!
//! Filer spike part A (docs/pilots/filer-spike.md, tests A1-A13). Not product code.
//!
//! One FAN_CLASS_PRE_CONTENT group, one FAN_MARK_FILESYSTEM mark with FAN_PRE_ACCESS on the
//! data volume. For every event: read the stub xattrs through the event fd, allow resident
//! ranges, otherwise apply the share policy (wait / failfast / waitquick:<ms>).
//!
//! Crash safety experiments:
//! * systemd fd store: the group fd is pushed with FDSTORE=1 / FDNAME=fanotify and reused on
//!   restart (LISTEN_FDS / LISTEN_FDNAMES), so the group outlives the process.
//! * journal: every event read is journalled (fd number, ino, range, file handle) before it is
//!   acted on, so a restarted instance can answer events its predecessor read but did not
//!   answer. The kernel matches a response to a pending event only by the fd integer
//!   (fs/notify/fanotify/fanotify_user.c, process_access_response()).
//! * private fill mount: the daemon can unshare its mount namespace and bind-mount the data
//!   volume at --fill-mount, with a FAN_MARK_MOUNT ignore mark, so it can open and fill files
//!   (for orphaned events) without generating events to itself.

use std::collections::{BTreeMap, HashSet};
use std::ffi::CString;
use std::fs::{File, OpenOptions};
use std::io::{self, BufRead, BufReader, Write};
use std::os::fd::{AsRawFd, RawFd};
use std::os::unix::ffi::OsStrExt;
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::mpsc;
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};

// ---- uapi constants (include/uapi/linux/fanotify.h, kernel 7.0) ----
const FAN_CLOEXEC: u32 = 0x1;
const FAN_CLASS_PRE_CONTENT: u32 = 0x8;
const FAN_UNLIMITED_QUEUE: u32 = 0x10;
const FAN_MARK_ADD: u32 = 0x1;
const FAN_MARK_MOUNT: u32 = 0x10;
const FAN_MARK_IGNORED_SURV_MODIFY: u32 = 0x40;
const FAN_MARK_FILESYSTEM: u32 = 0x100;
const FAN_MARK_EVICTABLE: u32 = 0x200;
const FAN_MARK_IGNORE: u32 = 0x400;
const FAN_PRE_ACCESS: u64 = 0x0010_0000;
const FAN_Q_OVERFLOW: u64 = 0x4000;
const FAN_ALLOW: u32 = 0x1;
const FAN_DENY: u32 = 0x2;
const FAN_EVENT_INFO_TYPE_RANGE: u8 = 6;
const FANOTIFY_METADATA_VERSION: u8 = 3;
const META_LEN: usize = 24;

fn deny_errno(e: i32) -> u32 {
    FAN_DENY | (((e as u32) & 0xff) << 24)
}

const XA_STATE: &str = "user.omnifile.state";
const XA_RESIDENT: &str = "user.omnifile.resident_bytes";
const XA_KEY: &str = "user.omnifile.key";

// ---- logging ----
fn ts() -> String {
    let d = SystemTime::now().duration_since(UNIX_EPOCH).unwrap();
    format!("{}.{:03}", d.as_secs(), d.subsec_millis())
}
macro_rules! log {
    ($($a:tt)*) => {{ eprintln!("[{}] {}", ts(), format!($($a)*)); }}
}

fn errno() -> i32 {
    io::Error::last_os_error().raw_os_error().unwrap_or(0)
}

// ---- config ----
#[derive(Clone)]
struct Cfg {
    mount: PathBuf,
    backend: PathBuf,
    state: PathBuf,
    fill_mount: Option<PathBuf>,
    private_ns: bool,
    unlimited: bool,
    ignore_filled: bool,
    fill_chunk: u64,
    pause_file: Option<PathBuf>,
    stall: bool,
    workers: usize,
    sweep: i32,
    no_journal: bool,
    ignore_pinned: bool,
    ignore_surv: bool,
    ignore_evfd: bool,
    verbose: bool,
}

fn parse_args() -> Cfg {
    let mut c = Cfg {
        mount: "/srv/omni-a/data".into(),
        backend: "/srv/omni-a/backend".into(),
        state: "/srv/omni-a/state".into(),
        fill_mount: None,
        private_ns: false,
        unlimited: true,
        ignore_filled: false,
        fill_chunk: 8 << 20,
        pause_file: None,
        stall: false,
        workers: 64,
        sweep: 4096,
        no_journal: false,
        ignore_pinned: false,
        ignore_surv: false,
        ignore_evfd: false,
        verbose: false,
    };
    let a: Vec<String> = std::env::args().collect();
    let mut i = 1;
    while i < a.len() {
        let v = |i: usize| a.get(i + 1).cloned().unwrap_or_default();
        match a[i].as_str() {
            "--mount" => { c.mount = v(i).into(); i += 1; }
            "--backend" => { c.backend = v(i).into(); i += 1; }
            "--state" => { c.state = v(i).into(); i += 1; }
            "--fill-mount" => { c.fill_mount = Some(v(i).into()); i += 1; }
            "--private-ns" => c.private_ns = true,
            "--no-unlimited" => c.unlimited = false,
            "--ignore-filled" => c.ignore_filled = true,
            "--fill-chunk" => { c.fill_chunk = v(i).parse().unwrap(); i += 1; }
            "--pause-file" => { c.pause_file = Some(v(i).into()); i += 1; }
            "--stall" => c.stall = true,
            "--workers" => { c.workers = v(i).parse().unwrap(); i += 1; }
            "--sweep" => { c.sweep = v(i).parse().unwrap(); i += 1; }
            "--no-journal" => c.no_journal = true,
            "--ignore-surv" => { c.ignore_filled = true; c.ignore_surv = true; }
            "--ignore-pinned" => { c.ignore_filled = true; c.ignore_pinned = true; }
            // the first, broken attempt (A13): plain FAN_MARK_IGNORE through the O_RDWR event fd.
            // Kept so the kernel canary can assert that the kernel still treats it as a no-op.
            "--ignore-evfd" => { c.ignore_filled = true; c.ignore_evfd = true; }
            "-v" | "--verbose" => c.verbose = true,
            x => { eprintln!("unknown arg {x}"); std::process::exit(2); }
        }
        i += 1;
    }
    c
}

// ---- small syscall helpers ----
fn cstr(p: &Path) -> CString {
    CString::new(p.as_os_str().as_bytes()).unwrap()
}

fn fgetxattr_str(fd: RawFd, name: &str) -> Option<String> {
    let n = CString::new(name).unwrap();
    let mut buf = [0u8; 512];
    let r = unsafe { libc::fgetxattr(fd, n.as_ptr(), buf.as_mut_ptr() as *mut _, buf.len()) };
    if r < 0 {
        return None;
    }
    Some(String::from_utf8_lossy(&buf[..r as usize]).trim().to_string())
}

fn fsetxattr_str(fd: RawFd, name: &str, val: &str) -> io::Result<()> {
    let n = CString::new(name).unwrap();
    let r = unsafe { libc::fsetxattr(fd, n.as_ptr(), val.as_ptr() as *const _, val.len(), 0) };
    if r < 0 { Err(io::Error::last_os_error()) } else { Ok(()) }
}

fn fstat(fd: RawFd) -> io::Result<libc::stat> {
    let mut st: libc::stat = unsafe { std::mem::zeroed() };
    if unsafe { libc::fstat(fd, &mut st) } < 0 {
        return Err(io::Error::last_os_error());
    }
    Ok(st)
}

/// First hole at or after `off` (st_size if none).
fn seek_hole(fd: RawFd, off: u64) -> Option<u64> {
    let r = unsafe { libc::lseek(fd, off as i64, libc::SEEK_HOLE) };
    if r < 0 { None } else { Some(r as u64) }
}
fn seek_data(fd: RawFd, off: u64) -> Option<u64> {
    let r = unsafe { libc::lseek(fd, off as i64, libc::SEEK_DATA) };
    if r < 0 { None } else { Some(r as u64) }
}

fn read_state_file(state: &Path, name: &str, default: &str) -> String {
    std::fs::read_to_string(state.join(name))
        .map(|s| s.trim().to_string())
        .unwrap_or_else(|_| default.to_string())
}

fn fd_path(fd: RawFd) -> String {
    std::fs::read_link(format!("/proc/self/fd/{fd}"))
        .map(|p| p.display().to_string())
        .unwrap_or_else(|_| "?".into())
}

fn comm_of(pid: i32) -> String {
    std::fs::read_to_string(format!("/proc/{pid}/comm"))
        .map(|s| s.trim().to_string())
        .unwrap_or_else(|_| "?".into())
}

// ---- file handles (for the journal) ----
#[repr(C)]
struct FileHandle {
    handle_bytes: u32,
    handle_type: i32,
    f_handle: [u8; 128],
}

fn handle_of(fd: RawFd) -> Option<String> {
    let mut fh = FileHandle { handle_bytes: 128, handle_type: 0, f_handle: [0; 128] };
    let mut mnt: i32 = 0;
    let empty = CString::new("").unwrap();
    let r = unsafe {
        libc::syscall(
            libc::SYS_name_to_handle_at,
            fd,
            empty.as_ptr(),
            &mut fh as *mut FileHandle,
            &mut mnt as *mut i32,
            libc::AT_EMPTY_PATH,
        )
    };
    if r < 0 {
        return None;
    }
    let hex: String = fh.f_handle[..fh.handle_bytes as usize].iter().map(|b| format!("{b:02x}")).collect();
    Some(format!("{}:{}", fh.handle_type, hex))
}

fn open_by_handle(mount_fd: RawFd, h: &str) -> io::Result<RawFd> {
    open_by_handle_flags(mount_fd, h, libc::O_RDWR)
}

fn open_by_handle_flags(mount_fd: RawFd, h: &str, flags: i32) -> io::Result<RawFd> {
    let (t, hex) = h.split_once(':').ok_or_else(|| io::Error::other("bad handle"))?;
    let mut fh = FileHandle { handle_bytes: (hex.len() / 2) as u32, handle_type: t.parse().unwrap_or(0), f_handle: [0; 128] };
    for i in 0..fh.handle_bytes as usize {
        fh.f_handle[i] = u8::from_str_radix(&hex[2 * i..2 * i + 2], 16).map_err(io::Error::other)?;
    }
    let r = unsafe { libc::syscall(libc::SYS_open_by_handle_at, mount_fd, &mut fh as *mut FileHandle, flags | libc::O_CLOEXEC) };
    if r < 0 { Err(io::Error::last_os_error()) } else { Ok(r as RawFd) }
}

// ---- sd_notify / fd store (implemented directly, no libsystemd) ----
fn sd_notify(msg: &str, fds: &[RawFd]) -> io::Result<bool> {
    let path = match std::env::var_os("NOTIFY_SOCKET") {
        Some(p) => p,
        None => return Ok(false),
    };
    let pb = path.as_bytes();
    unsafe {
        let s = libc::socket(libc::AF_UNIX, libc::SOCK_DGRAM | libc::SOCK_CLOEXEC, 0);
        if s < 0 {
            return Err(io::Error::last_os_error());
        }
        let mut addr: libc::sockaddr_un = std::mem::zeroed();
        addr.sun_family = libc::AF_UNIX as u16;
        for (i, b) in pb.iter().enumerate() {
            addr.sun_path[i] = if i == 0 && *b == b'@' { 0 } else { *b as libc::c_char };
        }
        let alen = (std::mem::size_of::<libc::sa_family_t>() + pb.len()) as u32;
        let mut iov = libc::iovec { iov_base: msg.as_ptr() as *mut _, iov_len: msg.len() };
        let mut mh: libc::msghdr = std::mem::zeroed();
        mh.msg_name = &mut addr as *mut _ as *mut _;
        mh.msg_namelen = alen;
        mh.msg_iov = &mut iov;
        mh.msg_iovlen = 1;
        let mut cbuf = vec![0u8; libc::CMSG_SPACE((fds.len() * 4) as u32) as usize];
        if !fds.is_empty() {
            mh.msg_control = cbuf.as_mut_ptr() as *mut _;
            mh.msg_controllen = cbuf.len();
            let cm = libc::CMSG_FIRSTHDR(&mh);
            (*cm).cmsg_level = libc::SOL_SOCKET;
            (*cm).cmsg_type = libc::SCM_RIGHTS;
            (*cm).cmsg_len = libc::CMSG_LEN((fds.len() * 4) as u32) as usize;
            std::ptr::copy_nonoverlapping(fds.as_ptr() as *const u8, libc::CMSG_DATA(cm), fds.len() * 4);
        }
        let r = libc::sendmsg(s, &mh, libc::MSG_NOSIGNAL);
        let e = io::Error::last_os_error();
        libc::close(s);
        if r < 0 {
            return Err(e);
        }
    }
    Ok(true)
}

/// Returns the fd passed by systemd under name `want`, if any.
fn listen_fd(want: &str) -> Option<RawFd> {
    let pid: i32 = std::env::var("LISTEN_PID").ok()?.parse().ok()?;
    if pid != unsafe { libc::getpid() } {
        return None;
    }
    let n: i32 = std::env::var("LISTEN_FDS").ok()?.parse().ok()?;
    let names = std::env::var("LISTEN_FDNAMES").unwrap_or_default();
    let names: Vec<&str> = names.split(':').collect();
    let mut found = None;
    for i in 0..n {
        let fd = 3 + i;
        let name = names.get(i as usize).copied().unwrap_or("");
        log!("systemd passed fd {fd} name={name:?} -> {}", fd_path(fd));
        if name == want && found.is_none() {
            found = Some(fd);
        }
    }
    found
}

// ---- journal ----
struct Journal {
    f: Mutex<File>,
}
impl Journal {
    fn append(&self, s: &str) {
        let mut f = self.f.lock().unwrap();
        let _ = f.write_all(s.as_bytes());
    }
}

#[derive(Clone, Debug)]
struct JRec {
    fd: i32,
    ino: u64,
    off: u64,
    len: u64,
    pid: i32,
    handle: String,
}

/// Parse journal: B <fd> <ino> <off> <len> <pid> <handle>, E <fd>. Returns unanswered records.
fn replay_journal(path: &Path) -> BTreeMap<i32, JRec> {
    let mut pending: BTreeMap<i32, JRec> = BTreeMap::new();
    if let Ok(f) = File::open(path) {
        for line in BufReader::new(f).lines().map_while(Result::ok) {
            let p: Vec<&str> = line.split_whitespace().collect();
            match p.as_slice() {
                ["B", fd, ino, off, len, pid, h] => {
                    let r = JRec {
                        fd: fd.parse().unwrap_or(-1),
                        ino: ino.parse().unwrap_or(0),
                        off: off.parse().unwrap_or(0),
                        len: len.parse().unwrap_or(0),
                        pid: pid.parse().unwrap_or(0),
                        handle: h.to_string(),
                    };
                    pending.insert(r.fd, r);
                }
                ["E", fd] => {
                    pending.remove(&fd.parse().unwrap_or(-1));
                }
                _ => {}
            }
        }
    }
    pending
}

// ---- shared state ----
struct G {
    cfg: Cfg,
    fan: RawFd,
    journal: Journal,
    fill_mount_fd: Option<RawFd>,
    stats_events: AtomicU64,
    stats_allowed_fast: AtomicU64,
    stats_filled: AtomicU64,
    stats_denied: AtomicU64,
    ignored_inodes: Mutex<HashSet<u64>>,
    inflight: AtomicU64,
    max_inflight: u64,
    filling: (Mutex<HashSet<(u64, u64)>>, std::sync::Condvar),
}

impl G {
    fn respond(&self, fd: i32, resp: u32) -> io::Result<()> {
        let buf: [u32; 2] = [fd as u32, resp];
        let r = unsafe { libc::write(self.fan, buf.as_ptr() as *const _, 8) };
        if r < 0 { Err(io::Error::last_os_error()) } else { Ok(()) }
    }
}

enum Policy {
    Wait,
    FailFast,
    WaitQuick(u64),
}

fn policy(g: &G) -> Policy {
    let p = read_state_file(&g.cfg.state, "policy", "wait");
    if p == "failfast" {
        Policy::FailFast
    } else if let Some(ms) = p.strip_prefix("waitquick:") {
        Policy::WaitQuick(ms.parse().unwrap_or(0))
    } else {
        Policy::Wait
    }
}

/// Simulated backend: delay_ms before first byte, rate_mbps (0 = unlimited).
fn eta_ms(g: &G, bytes: u64) -> u64 {
    let delay: u64 = read_state_file(&g.cfg.state, "delay_ms", "0").parse().unwrap_or(0);
    let rate: u64 = read_state_file(&g.cfg.state, "rate_mbps", "0").parse().unwrap_or(0);
    delay + if rate > 0 { bytes * 1000 / (rate << 20) } else { 0 }
}

// pattern for synthetic backend objects (same as omnitool)
const PAT: u64 = 0x4F4D_4E49_4649_4C45; // "OMNIFILE"
fn pattern_fill(buf: &mut [u8], off: u64, seed: u64) {
    // buf must start at 8-byte aligned offset
    for (i, ch) in buf.chunks_mut(8).enumerate() {
        let o = off + (i as u64) * 8;
        let w = (o ^ PAT ^ seed).to_le_bytes();
        ch.copy_from_slice(&w[..ch.len()]);
    }
}

enum Decision {
    Allow,
    Fill { from: u64, to: u64, key: String, size: u64 },
}

/// Decide whether [off, off+len) of the open file needs filling.
fn classify(g: &G, fd: RawFd, off: u64, len: u64) -> io::Result<Decision> {
    if len == u64::MAX {
        // truncate: allowed. (Product: a shrink keeps the stub; truncate-to-0 should clear it.)
        return Ok(Decision::Allow);
    }
    let state = fgetxattr_str(fd, XA_STATE);
    if state.as_deref() != Some("stub") {
        return Ok(Decision::Allow);
    }
    let st = fstat(fd)?;
    let size = st.st_size as u64;
    let resident: u64 = fgetxattr_str(fd, XA_RESIDENT).and_then(|s| s.parse().ok()).unwrap_or(0);
    let end = if len == 0 { size } else { (off.saturating_add(len)).min(size) };
    let start = off.max(resident);
    if start >= end {
        return Ok(Decision::Allow);
    }
    // Already filled (no hole in [start, end))?
    match seek_hole(fd, start) {
        Some(h) if h >= end => return Ok(Decision::Allow),
        _ => {}
    }
    let key = fgetxattr_str(fd, XA_KEY).ok_or_else(|| io::Error::other("stub without key"))?;
    let ch = g.cfg.fill_chunk.max(4096);
    let from = (start / ch * ch).max(resident);
    let to = end.div_ceil(ch).saturating_mul(ch).min(size);
    Ok(Decision::Fill { from, to, key, size })
}

/// Copy backend bytes into the holes of [from, to) through `fd` (an event fd, FMODE_NONOTIFY,
/// or an fd opened through the ignored fill mount).
/// delay_scope=file: the simulated recall latency applies only to the first fill of a stub
/// (nothing beyond resident_bytes is present yet); later range fills are immediate.
fn already_recalling(g: &G, fd: RawFd, size: u64) -> bool {
    if read_state_file(&g.cfg.state, "delay_scope", "fill") != "file" {
        return false;
    }
    let resident: u64 = fgetxattr_str(fd, XA_RESIDENT).and_then(|s| s.parse().ok()).unwrap_or(0);
    matches!(seek_data(fd, resident), Some(d) if d < size)
}

fn fill(g: &G, fd: RawFd, from: u64, to: u64, key: &str, size: u64) -> io::Result<u64> {
    let eta = if already_recalling(g, fd, size) { 0 } else { eta_ms(g, to - from) };
    if eta > 0 {
        std::thread::sleep(Duration::from_millis(eta));
    }
    let synth: Option<u64> = key.strip_prefix("synthetic:").map(|s| s.parse().unwrap_or(0));
    let backend = if synth.is_none() {
        Some(File::open(g.cfg.backend.join(key))?)
    } else {
        None
    };
    let mut buf = vec![0u8; 1 << 20];
    let mut written = 0u64;
    let mut pos = from;
    while pos < to {
        // find next hole in [pos, to)
        let hole = match seek_hole(fd, pos) { Some(h) => h, None => break };
        if hole >= to { break; }
        let data = seek_data(fd, hole).unwrap_or(size).min(to);
        let mut p = hole;
        while p < data {
            let n = ((data - p) as usize).min(buf.len());
            let b = &mut buf[..n];
            if let Some(seed) = synth {
                // keep 8-byte alignment of the pattern
                let base = p & !7;
                let mut tmp = vec![0u8; n + 16];
                pattern_fill(&mut tmp, base, seed);
                let skip = (p - base) as usize;
                b.copy_from_slice(&tmp[skip..skip + n]);
            } else {
                let r = unsafe { libc::pread(backend.as_ref().unwrap().as_raw_fd(), b.as_mut_ptr() as *mut _, n, p as i64) };
                if r != n as isize {
                    return Err(io::Error::other(format!("short backend read at {p}: {r}")));
                }
            }
            let w = unsafe { libc::pwrite(fd, b.as_ptr() as *const _, n, p as i64) };
            if w != n as isize {
                return Err(io::Error::last_os_error());
            }
            p += n as u64;
            written += n as u64;
        }
        pos = data;
    }
    // fully resident?
    let resident: u64 = fgetxattr_str(fd, XA_RESIDENT).and_then(|s| s.parse().ok()).unwrap_or(0);
    if seek_hole(fd, resident).map(|h| h >= size).unwrap_or(false) {
        unsafe { libc::fdatasync(fd) };
        fsetxattr_str(fd, XA_RESIDENT, &size.to_string())?;
        fsetxattr_str(fd, XA_STATE, "resident")?;
        log!("file now fully resident ({} bytes): {}", size, fd_path(fd));
    }
    Ok(written)
}

/// Add an inode ignore mark for PRE_ACCESS on a fully resident file.
///
/// Kernel 7.0 do_fanotify_mark() silently returns 0 WITHOUT adding a non-SURV_MODIFY ignore mark
/// when the inode is open for write -- and our event fd is O_RDWR, so marking through it is a
/// no-op. Default: after the event fd is closed, reopen the file read-only through the ignored
/// fill mount and add a plain FAN_MARK_IGNORE (cleared by the kernel on the next modification).
/// --ignore-surv: FAN_MARK_IGNORE_SURV through the event fd (survives modification; the daemon
/// must remove it before re-stubbing the file).
fn add_ignore_mark(g: &G, fd: RawFd, handle: Option<String>) {
    let ino = match fstat(fd) { Ok(s) => s.st_ino, Err(_) => return };
    if !g.ignored_inodes.lock().unwrap().insert(ino) {
        return;
    }
    let ev = if g.cfg.ignore_pinned { 0 } else { FAN_MARK_EVICTABLE };
    let (target, flags, how) = if g.cfg.ignore_surv {
        (fd, FAN_MARK_ADD | FAN_MARK_IGNORE | FAN_MARK_IGNORED_SURV_MODIFY | ev, "IGNORE_SURV via event fd")
    } else if g.cfg.ignore_evfd {
        (fd, FAN_MARK_ADD | FAN_MARK_IGNORE | ev, "IGNORE via event fd")
    } else {
        let (Some(mfd), Some(h)) = (g.fill_mount_fd, handle) else { return };
        match open_by_handle_flags(mfd, &h, libc::O_RDONLY) {
            Ok(ro) => (ro, FAN_MARK_ADD | FAN_MARK_IGNORE | ev, "IGNORE via read-only fill-mount fd"),
            Err(e) => { log!("ignore mark: reopen failed: {e}"); return; }
        }
    };
    let r = unsafe { libc::fanotify_mark(g.fan, flags, FAN_PRE_ACCESS, target, std::ptr::null()) };
    let err = io::Error::last_os_error();
    if target != fd { unsafe { libc::close(target) }; }
    let fi = std::fs::read_to_string(format!("/proc/self/fdinfo/{}", g.fan)).unwrap_or_default();
    let present = fi.lines().any(|l| l.contains(&format!(" ino:{ino:x} ")));
    if r < 0 {
        log!("ignore mark on ino {ino} failed: {err}");
    } else {
        log!("ignore mark on ino {ino} ({how}, {}): rc=0, present in fdinfo: {present}", if ev != 0 { "evictable" } else { "pinned" });
    }
    if !present { g.ignored_inodes.lock().unwrap().remove(&ino); }
}

struct Job {
    fd: i32,          // event fd number (the response key)
    file_fd: RawFd,   // fd to access the file through (== fd, or a fill-mount fd for orphans)
    pid: i32,
    off: u64,
    len: u64,
    orphan: bool,
    reserved: bool, // j.fd is a /dev/null placeholder we own
}

fn handle_slow(g: &'static G, j: Job) {
    let t0 = Instant::now();
    let dec = classify(g, j.file_fd, j.off, j.len);
    let resp = match dec {
        Ok(Decision::Allow) => FAN_ALLOW,
        Ok(Decision::Fill { from, to, key, size }) => {
            let pol = policy(g);
            let do_wait = match pol {
                Policy::Wait => true,
                Policy::FailFast => false,
                Policy::WaitQuick(ms) => {
                    let e = if already_recalling(g, j.file_fd, size) { 0 } else { eta_ms(g, to - from) };
                    log!("waitquick: eta {e} ms vs threshold {ms} ms");
                    e <= ms
                }
            };
            if do_wait {
                let ino = fstat(j.file_fd).map(|s| s.st_ino).unwrap_or(0);
                let k = (ino, from);
                {
                    let (m, cv) = &g.filling;
                    let mut set = m.lock().unwrap();
                    let mut waited = false;
                    while set.contains(&k) {
                        set = cv.wait(set).unwrap();
                        waited = true;
                    }
                    if waited {
                        drop(set);
                        // someone else filled this chunk: re-evaluate from scratch
                        return handle_slow(g, j);
                    }
                    set.insert(k);
                }
                let r = fill(g, j.file_fd, from, to, &key, size);
                {
                    let (m, cv) = &g.filling;
                    m.lock().unwrap().remove(&k);
                    cv.notify_all();
                }
                match r {
                    Ok(n) => {
                        g.stats_filled.fetch_add(1, Ordering::Relaxed);
                        log!("filled {} bytes [{from},{to}) key={key} pid={} ({}) in {:?}; allow", n, j.pid, comm_of(j.pid), t0.elapsed());
                        FAN_ALLOW
                    }
                    Err(e) => {
                        log!("fill failed key={key}: {e}; deny EIO");
                        deny_errno(libc::EIO)
                    }
                }
            } else {
                g.stats_denied.fetch_add(1, Ordering::Relaxed);
                let line = format!(
                    "{} RECALL-QUEUED key={} range=[{},{}) pid={} comm={} path={}\n",
                    ts(), key, from, to, j.pid, comm_of(j.pid), fd_path(j.file_fd)
                );
                log!("failfast: {}", line.trim());
                let _ = OpenOptions::new().create(true).append(true).open(g.cfg.state.join("recalls.log")).and_then(|mut f| f.write_all(line.as_bytes()));
                // background recall so that a retry succeeds later
                let bg = unsafe { libc::dup(j.file_fd) };
                if bg >= 0 {
                    std::thread::spawn(move || {
                        let r = fill(g, bg, from, to, &key, size);
                        log!("background recall key={key} [{from},{to}) done: {:?}", r.map(|_| ()));
                        unsafe { libc::close(bg) };
                    });
                }
                deny_errno(libc::EIO)
            }
        }
        Err(e) => {
            log!("classify error on fd {} ({}): {e}; deny EIO", j.fd, fd_path(j.file_fd));
            deny_errno(libc::EIO)
        }
    };
    let want_ignore = resp == FAN_ALLOW && g.cfg.ignore_filled
        && fgetxattr_str(j.file_fd, XA_STATE).as_deref() == Some("resident");
    if want_ignore && (g.cfg.ignore_surv || g.cfg.ignore_evfd) {
        add_ignore_mark(g, j.file_fd, None);
        finish(g, &j, resp);
    } else if want_ignore {
        let h = handle_of(j.file_fd);
        let keep = unsafe { libc::dup(j.file_fd) }; // for fstat only; closed below
        finish(g, &j, resp);
        // the event fd is closed now; our dup still holds the file open for write, so fstat
        // through it and close it before marking
        let st_fd = keep;
        let ino_fd = unsafe { libc::open(format!("/proc/self/fd/{st_fd}\0").as_ptr() as *const _, libc::O_PATH) };
        unsafe { libc::close(st_fd) };
        if ino_fd >= 0 {
            add_ignore_mark(g, ino_fd, h);
            unsafe { libc::close(ino_fd) };
        }
    } else {
        finish(g, &j, resp);
    }
}

fn finish(g: &G, j: &Job, resp: u32) {
    g.inflight.fetch_sub(1, Ordering::Relaxed);
    if !g.cfg.stall {
        match g.respond(j.fd, resp) {
            Ok(()) => {}
            Err(e) => log!("response for fd {} failed: {e}{}", j.fd, if j.orphan { " (orphan already answered?)" } else { "" }),
        }
        g.journal.append(&format!("E {}\n", j.fd));
    }
    if g.cfg.verbose {
        log!("answered fd={} resp={:#x} orphan={}", j.fd, resp, j.orphan);
    }
    if !g.cfg.stall {
        unsafe { libc::close(j.file_fd) };
        if j.reserved {
            // release the fd-number reservation
            unsafe { libc::close(j.fd) };
        }
    }
}

fn main() {
    let cfg = parse_args();
    std::fs::create_dir_all(&cfg.state).ok();
    log!("omnifile-guard spike starting pid={} mount={} unlimited={} ignore_filled={} fill_mount={:?} private_ns={}",
        std::process::id(), cfg.mount.display(), cfg.unlimited, cfg.ignore_filled, cfg.fill_mount, cfg.private_ns);

    // 1. private mount namespace with the unwatched fill mount
    if cfg.private_ns {
        let fm = cfg.fill_mount.clone().expect("--private-ns needs --fill-mount");
        unsafe {
            if libc::unshare(libc::CLONE_NEWNS) < 0 {
                log!("unshare failed: {}", io::Error::last_os_error());
                std::process::exit(1);
            }
            let root = CString::new("/").unwrap();
            if libc::mount(std::ptr::null(), root.as_ptr(), std::ptr::null(), libc::MS_REC | libc::MS_SLAVE, std::ptr::null()) < 0 {
                log!("make-rslave failed: {}", io::Error::last_os_error());
            }
            std::fs::create_dir_all(&fm).ok();
            if libc::mount(cstr(&cfg.mount).as_ptr(), cstr(&fm).as_ptr(), std::ptr::null(), libc::MS_BIND, std::ptr::null()) < 0 {
                log!("bind mount failed: {}", io::Error::last_os_error());
                std::process::exit(1);
            }
        }
        log!("private mount namespace: {} bound at {}", cfg.mount.display(), fm.display());
    }

    // 2. group: reuse from fd store, or create + arm
    let mut reused = false;
    let fan: RawFd = match listen_fd("fanotify") {
        Some(fd) => {
            reused = true;
            let fdinfo = std::fs::read_to_string(format!("/proc/self/fdinfo/{fd}")).unwrap_or_default();
            log!("REUSING fanotify group fd {fd} from systemd fd store; fdinfo:\n{}", fdinfo.trim());
            unsafe { libc::fcntl(fd, libc::F_SETFD, libc::FD_CLOEXEC) };
            fd
        }
        None => {
            let mut flags = FAN_CLASS_PRE_CONTENT | FAN_CLOEXEC;
            if cfg.unlimited {
                flags |= FAN_UNLIMITED_QUEUE;
            }
            let fd = unsafe { libc::fanotify_init(flags, (libc::O_RDWR | libc::O_LARGEFILE) as u32) };
            if fd < 0 {
                log!("fanotify_init failed: {}", io::Error::last_os_error());
                std::process::exit(1);
            }
            let r = unsafe {
                libc::fanotify_mark(fd, FAN_MARK_ADD | FAN_MARK_FILESYSTEM, FAN_PRE_ACCESS, libc::AT_FDCWD, cstr(&cfg.mount).as_ptr())
            };
            if r < 0 {
                log!("fanotify_mark(FILESYSTEM, PRE_ACCESS) failed: {}", io::Error::last_os_error());
                std::process::exit(1);
            }
            log!("created group fd {fd} (flags {flags:#x}) and armed FAN_MARK_FILESYSTEM|FAN_PRE_ACCESS on {}", cfg.mount.display());
            fd
        }
    };

    // 3. ignore mark on our fill mount (always: the mount object is new in each namespace)
    let mut fill_mount_fd = None;
    if let Some(fm) = &cfg.fill_mount {
        let r = unsafe {
            libc::fanotify_mark(fan, FAN_MARK_ADD | FAN_MARK_MOUNT | FAN_MARK_IGNORE | FAN_MARK_IGNORED_SURV_MODIFY, FAN_PRE_ACCESS, libc::AT_FDCWD, cstr(fm).as_ptr())
        };
        if r < 0 {
            log!("mount ignore mark on {} failed: {}", fm.display(), io::Error::last_os_error());
            std::process::exit(1);
        }
        let mfd = unsafe { libc::open(cstr(fm).as_ptr(), libc::O_RDONLY | libc::O_DIRECTORY | libc::O_CLOEXEC) };
        fill_mount_fd = Some(mfd);
        log!("mount ignore mark armed on fill mount {}", fm.display());
    }

    // 4. push to fd store if newly created and running under systemd
    if !reused {
        match sd_notify("FDSTORE=1\nFDNAME=fanotify\nFDPOLL=0", &[fan]) {
            Ok(true) => log!("pushed group fd {fan} to systemd fd store (FDNAME=fanotify)"),
            Ok(false) => log!("NOTIFY_SOCKET not set: running WITHOUT an fd holder (group dies with this process)"),
            Err(e) => log!("FDSTORE push failed: {e}"),
        }
    }

    // raise RLIMIT_NOFILE to the hard limit; bound in-flight events well below it, because an
    // event whose fd cannot be created is DENIED by the kernel (EPERM to the reader).
    let mut rl = libc::rlimit { rlim_cur: 0, rlim_max: 0 };
    unsafe { libc::getrlimit(libc::RLIMIT_NOFILE, &mut rl) };
    rl.rlim_cur = rl.rlim_max;
    unsafe { libc::setrlimit(libc::RLIMIT_NOFILE, &rl) };
    let max_inflight = (rl.rlim_cur as u64).saturating_sub(256).max(16);
    log!("RLIMIT_NOFILE={} -> max in-flight events {}", rl.rlim_cur, max_inflight);

    // 5. journal replay
    let jpath = cfg.state.join("journal");
    let pending = if reused { replay_journal(&jpath) } else { BTreeMap::new() };
    if !reused && jpath.exists() {
        let _ = std::fs::rename(&jpath, cfg.state.join("journal.stale"));
    }
    let jfile = OpenOptions::new().create(true).write(true).truncate(true).open(&jpath).expect("journal");
    // (the journal file is created before any event fd, so its number is never an event number
    // of *this* process)
    let g: &'static G = Box::leak(Box::new(G {
        cfg: cfg.clone(),
        fan,
        journal: Journal { f: Mutex::new(jfile) },
        fill_mount_fd,
        stats_events: AtomicU64::new(0),
        stats_allowed_fast: AtomicU64::new(0),
        stats_filled: AtomicU64::new(0),
        stats_denied: AtomicU64::new(0),
        ignored_inodes: Mutex::new(HashSet::new()),
        inflight: AtomicU64::new(0),
        max_inflight,
        filling: (Mutex::new(HashSet::new()), std::sync::Condvar::new()),
    }));
    // re-journal still-pending orphans so a second crash does not lose them
    for r in pending.values() {
        g.journal.append(&format!("B {} {} {} {} {} {}\n", r.fd, r.ino, r.off, r.len, r.pid, r.handle));
    }

    // 6. reserve orphan fd numbers so new events cannot reuse them before they are answered
    let devnull = CString::new("/dev/null").unwrap();
    let mut orphan_jobs = vec![];
    let mut reserved_set = HashSet::new();
    for r in pending.values() {
        let busy = unsafe { libc::fcntl(r.fd, libc::F_GETFD) } >= 0;
        if !busy {
            let t = unsafe { libc::open(devnull.as_ptr(), libc::O_RDONLY) };
            unsafe { libc::dup2(t, r.fd) };
            unsafe { libc::close(t) };
            reserved_set.insert(r.fd);
        }
        log!("ORPHAN event fd={} ino={} range=[{},+{}) pid={} ({}); fd number reserved{}",
            r.fd, r.ino, r.off, r.len, r.pid, comm_of(r.pid), if busy { " (already in use here)" } else { "" });
        orphan_jobs.push(r.clone());
    }

    // 7. sweep unknown orphans (read by the predecessor but lost before journalling): deny them
    if reused && cfg.sweep > 0 {
        let mut swept = 0;
        for n in 0..cfg.sweep {
            if pending.contains_key(&n) {
                continue;
            }
            if g.respond(n, deny_errno(libc::EIO)).is_ok() {
                swept += 1;
                log!("sweep: unjournalled pending event fd={n} denied with EIO");
            }
        }
        log!("orphan sweep over fd 0..{}: {} unjournalled events denied", cfg.sweep, swept);
    }

    // 8. worker pool
    let (tx, rx) = mpsc::channel::<Job>();
    let rx = Arc::new(Mutex::new(rx));
    for _ in 0..cfg.workers {
        let rx = rx.clone();
        std::thread::spawn(move || loop {
            let j = { rx.lock().unwrap().recv() };
            match j {
                Ok(j) => handle_slow(g, j),
                Err(_) => return,
            }
        });
    }

    // orphans: open through the ignored fill mount
    for r in orphan_jobs {
        let file_fd = match g.fill_mount_fd {
            Some(mfd) => open_by_handle(mfd, &r.handle),
            None => Err(io::Error::other("no fill mount")),
        };
        match file_fd {
            Ok(ffd) => {
                let ino = fstat(ffd).map(|s| s.st_ino).unwrap_or(0);
                if ino != r.ino {
                    log!("orphan fd={} handle resolves to ino {ino}, expected {}; deny", r.fd, r.ino);
                    unsafe { libc::close(ffd) };
                    let _ = g.respond(r.fd, deny_errno(libc::EIO));
                    continue;
                }
                log!("orphan fd={} reopened via fill mount as fd {ffd}; re-evaluating", r.fd);
                g.inflight.fetch_add(1, Ordering::Relaxed);
                tx.send(Job { fd: r.fd, file_fd: ffd, pid: r.pid, off: r.off, len: r.len, orphan: true, reserved: reserved_set.contains(&r.fd) }).unwrap();
            }
            Err(e) => {
                log!("orphan fd={}: cannot reopen ({e}); deny EIO (fail closed)", r.fd);
                let _ = g.respond(r.fd, deny_errno(libc::EIO));
                g.journal.append(&format!("E {}\n", r.fd));
            }
        }
    }

    // 9. ready
    match sd_notify(&format!("READY=1\nSTATUS=armed on {} (reused={reused})", cfg.mount.display()), &[]) {
        Ok(true) => log!("READY=1 sent"),
        _ => {}
    }
    log!("guard armed and serving events");

    // stats thread
    std::thread::spawn(move || loop {
        std::thread::sleep(Duration::from_secs(30));
        log!("stats: events={} fast_allow={} filled={} denied={}",
            g.stats_events.load(Ordering::Relaxed), g.stats_allowed_fast.load(Ordering::Relaxed),
            g.stats_filled.load(Ordering::Relaxed), g.stats_denied.load(Ordering::Relaxed));
    });

    // 10. event loop
    let mut buf = vec![0u64; 8192]; // 64 KiB, 8-byte aligned
    loop {
        if let Some(pf) = &cfg.pause_file {
            if pf.exists() {
                std::thread::sleep(Duration::from_millis(200));
                continue;
            }
        }
        while g.inflight.load(Ordering::Relaxed) >= g.max_inflight {
            std::thread::sleep(Duration::from_millis(5));
        }
        let n = unsafe { libc::read(fan, buf.as_mut_ptr() as *mut _, buf.len() * 8) };
        if n < 0 {
            let e = errno();
            if e == libc::EINTR || e == libc::EAGAIN {
                continue;
            }
            if e == libc::EMFILE || e == libc::ENFILE {
                // the kernel has already DENIED the event it could not open an fd for
                log!("read(fanotify): {} -- one event was denied (EPERM) by the kernel; backing off", io::Error::from_raw_os_error(e));
                std::thread::sleep(Duration::from_millis(20));
                continue;
            }
            log!("read(fanotify) failed: {}", io::Error::from_raw_os_error(e));
            std::process::exit(1);
        }
        let bytes = unsafe { std::slice::from_raw_parts(buf.as_ptr() as *const u8, n as usize) };
        // parse all events of this batch
        struct Ev { fd: i32, pid: i32, mask: u64, off: u64, len: u64 }
        let mut evs = vec![];
        let mut p = 0usize;
        while p + META_LEN <= bytes.len() {
            let rd32 = |o: usize| u32::from_ne_bytes(bytes[o..o + 4].try_into().unwrap());
            let rd64 = |o: usize| u64::from_ne_bytes(bytes[o..o + 8].try_into().unwrap());
            let event_len = rd32(p) as usize;
            let vers = bytes[p + 4];
            let meta_len = u16::from_ne_bytes([bytes[p + 6], bytes[p + 7]]) as usize;
            let mask = rd64(p + 8);
            let fd = rd32(p + 16) as i32;
            let pid = rd32(p + 20) as i32;
            if vers != FANOTIFY_METADATA_VERSION || event_len < META_LEN {
                log!("unexpected metadata vers={vers} len={event_len}");
                break;
            }
            let (mut off, mut len) = (0u64, 0u64);
            let mut has_range = false;
            let mut q = p + meta_len;
            while q + 4 <= p + event_len {
                let it = bytes[q];
                let il = u16::from_ne_bytes([bytes[q + 2], bytes[q + 3]]) as usize;
                if it == FAN_EVENT_INFO_TYPE_RANGE && il >= 24 {
                    off = rd64(q + 8);
                    len = rd64(q + 16);
                    has_range = true;
                }
                if il == 0 { break; }
                q += il;
            }
            if mask & FAN_Q_OVERFLOW != 0 {
                log!("!!! FAN_Q_OVERFLOW received: the kernel dropped (= allowed) permission events");
            }
            if has_range && len == 0 && off > 0 {
                // truncate(2) to a non-zero length: fsnotify_truncate_perm() reports (len, 0).
                // Not a data access; mark with len = u64::MAX sentinel.
                len = u64::MAX;
            }
            // (0, 0) is ambiguous: truncate-to-0 / O_TRUNC, OR a whole-file clone
            // (FICLONE / remap with len 0, fs/remap_range.c remap_verify_area()). Treat it as a
            // whole-file access (len 0 => whole file in classify()): safe, possibly wasteful.
            evs.push(Ev { fd, pid, mask, off, len });
            p += event_len;
        }
        // journal the batch before acting on any of it
        let mut jb = String::new();
        for e in &evs {
            if e.fd < 0 { continue; }
            let ino = fstat(e.fd).map(|s| s.st_ino).unwrap_or(0);
            let h = handle_of(e.fd).unwrap_or_else(|| "-".into());
            jb.push_str(&format!("B {} {} {} {} {} {}\n", e.fd, ino, e.off, e.len, e.pid, h));
        }
        if !cfg.no_journal {
            g.journal.append(&jb);
        }
        g.stats_events.fetch_add(evs.len() as u64, Ordering::Relaxed);
        g.inflight.fetch_add(evs.iter().filter(|e| e.fd >= 0).count() as u64, Ordering::Relaxed);
        if cfg.stall {
            for e in &evs {
                log!("STALL: read event fd={} pid={} range=[{},+{}) {} -- not answering", e.fd, e.pid, e.off, e.len, fd_path(e.fd));
            }
            continue;
        }
        let mut done = String::new();
        for e in evs {
            if e.fd < 0 {
                continue;
            }
            if cfg.verbose {
                log!("event fd={} mask={:#x} pid={} ({}) range=[{},+{}) {}", e.fd, e.mask, e.pid, comm_of(e.pid), e.off, e.len, fd_path(e.fd));
            }
            // fast path inline: not a stub, or resident range
            match classify(g, e.fd, e.off, e.len) {
                Ok(Decision::Allow) if !cfg.ignore_filled => {
                    let _ = g.respond(e.fd, FAN_ALLOW);
                    unsafe { libc::close(e.fd) };
                    g.stats_allowed_fast.fetch_add(1, Ordering::Relaxed);
                    g.inflight.fetch_sub(1, Ordering::Relaxed);
                    done.push_str(&format!("E {}\n", e.fd));
                }
                _ => {
                    tx.send(Job { fd: e.fd, file_fd: e.fd, pid: e.pid, off: e.off, len: e.len, orphan: false, reserved: false }).unwrap();
                }
            }
        }
        if !done.is_empty() {
            g.journal.append(&done);
        }
    }
}
