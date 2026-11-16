//! smbguard-tool: A-suite helpers (the filer spike's omnitool, ported to the
//! production `trusted.omnifile` marker).
//!
//! The test pattern: the little-endian u64 at every 8-aligned offset `o` is
//! `o ^ "OMNIFILE" ^ seed`; a stub's seed is its marker's `file_id`, which is
//! what the harness's synthetic backend serves. Any zero page or misplaced
//! block is detected.
//!
//! Exit status of verifying commands: 0 OK, 2 ZEROS, 3 MISMATCH, 4 ERROR.

#[cfg(not(target_os = "linux"))]
fn main() {
    eprintln!("smbguard-tool runs on Linux only");
    std::process::exit(2);
}

#[cfg(target_os = "linux")]
fn main() {
    linux::main()
}

#[cfg(target_os = "linux")]
mod linux {
    use std::ffi::CString;
    use std::io;
    use std::os::fd::BorrowedFd;
    use std::os::unix::ffi::OsStrExt;
    use std::path::Path;
    use std::sync::Arc;
    use std::sync::atomic::{AtomicU64, Ordering};
    use std::time::{Duration, Instant};

    use omnifile_marker::xattr;
    use omnifile_marker::{Flags, Marker};

    const PAT: u64 = 0x4F4D_4E49_4649_4C45;

    fn pattern_fill(buf: &mut [u8], off: u64, seed: u64) {
        let base = off & !7;
        let skip = (off - base) as usize;
        let mut tmp = vec![0u8; buf.len() + 16];
        for (i, ch) in tmp.chunks_mut(8).enumerate() {
            let w = ((base + 8 * i as u64) ^ PAT ^ seed).to_le_bytes();
            ch.copy_from_slice(&w[..ch.len()]);
        }
        buf.copy_from_slice(&tmp[skip..skip + buf.len()]);
    }

    fn kernel_check(dir: &str) -> i32 {
        use omnifile_smbguard::linux::sys;
        use omnifile_smbguard::uapi::{
            self, FAN_CLASS_PRE_CONTENT, FAN_CLOEXEC, FAN_MARK_ADD, FAN_PRE_ACCESS,
        };
        use std::os::fd::AsRawFd;
        let fail = |what: &str, e: io::Error| {
            println!("kernel-check: FAIL: {what}: {e}");
            5
        };
        let _ = std::fs::create_dir_all(dir);
        let probe = format!("{dir}/probe");
        if let Err(e) = std::fs::write(&probe, b"omnifile kernel check\n") {
            return fail("write the probe file", e);
        }
        let group =
            match sys::fanotify_init(FAN_CLASS_PRE_CONTENT | FAN_CLOEXEC, libc::O_RDONLY as u32) {
                Ok(g) => g,
                Err(e) => return fail("fanotify_init(FAN_CLASS_PRE_CONTENT)", e),
            };
        let path = cpath(&probe);
        if let Err(e) = sys::fanotify_mark(
            group.as_raw_fd(),
            FAN_MARK_ADD,
            FAN_PRE_ACCESS,
            libc::AT_FDCWD,
            Some(&path),
        ) {
            return fail("fanotify_mark(FAN_PRE_ACCESS)", e);
        }
        // One read: a read-to-end would raise a second event.
        let reader = std::thread::spawn(move || {
            use std::io::Read;
            let mut b = [0u8; 64];
            std::fs::File::open(&probe)
                .and_then(|mut f| f.read(&mut b))
                .map(|_| ())
                .map_err(|e| e.raw_os_error().unwrap_or(0))
        });
        // Answer the first event with EIO, as the guard does for a hole, and
        // any later one with ALLOW, until the reader is done: a boot gate
        // must end, whatever the kernel does.
        let mut buf = vec![0u8; 4096];
        let deadline = Instant::now() + Duration::from_secs(10);
        let mut answered = false;
        while !reader.is_finished() && Instant::now() < deadline {
            let mut pfd = libc::pollfd {
                fd: group.as_raw_fd(),
                events: libc::POLLIN,
                revents: 0,
            };
            // SAFETY: one valid pollfd.
            if unsafe { libc::poll(&mut pfd, 1, 500) } <= 0 {
                continue;
            }
            // SAFETY: buf is valid for its length.
            let n = unsafe { libc::read(group.as_raw_fd(), buf.as_mut_ptr() as *mut _, buf.len()) };
            if n <= 0 {
                continue;
            }
            let (evs, _) = uapi::parse(&buf[..n as usize]);
            for ev in evs {
                if ev.fd >= 0 {
                    let answer = if answered {
                        uapi::FAN_ALLOW
                    } else {
                        // The answer under test.
                        uapi::deny_errno(libc::EIO)
                    };
                    let _ = sys::respond(group.as_raw_fd(), ev.fd, answer);
                    // SAFETY: the kernel opened this descriptor for us.
                    unsafe { libc::close(ev.fd) };
                    answered = true;
                }
            }
        }
        if !answered {
            return fail(
                "no pre-content event within 10 s",
                io::Error::other("timeout"),
            );
        }
        if !reader.is_finished() {
            // Exiting closes the group, which releases the read.
            return fail(
                "the read did not end within 10 s",
                io::Error::other("timeout"),
            );
        }
        match reader.join() {
            Ok(Err(e)) if e == libc::EIO => {
                println!("kernel-check: ok (pre-content event held the read; EIO answer honoured)");
                0
            }
            Ok(Err(e)) => fail(
                "the read failed with another error",
                io::Error::from_raw_os_error(e),
            ),
            Ok(Ok(())) => {
                println!("kernel-check: FAIL: the read succeeded despite the EIO answer");
                5
            }
            Err(_) => fail("reader thread", io::Error::other("panicked")),
        }
    }

    fn cpath(p: &str) -> CString {
        CString::new(Path::new(p).as_os_str().as_bytes()).unwrap()
    }

    fn open(p: &str, flags: i32) -> io::Result<i32> {
        // SAFETY: NUL-terminated path.
        let fd = unsafe { libc::open(cpath(p).as_ptr(), flags | libc::O_CLOEXEC, 0o644) };
        if fd < 0 {
            Err(io::Error::last_os_error())
        } else {
            Ok(fd)
        }
    }

    fn bfd(fd: i32) -> BorrowedFd<'static> {
        // SAFETY: the caller keeps fd open while the borrow is used.
        unsafe { BorrowedFd::borrow_raw(fd) }
    }

    /// Create `path` with `size` bytes, the pattern written over `[0, upto)`.
    fn gen_file(path: &str, size: u64, seed: u64, upto: u64) -> i32 {
        let _ = std::fs::remove_file(path);
        let fd = open(path, libc::O_RDWR | libc::O_CREAT | libc::O_EXCL).expect("create");
        // SAFETY: plain syscalls on our fd and buffer.
        unsafe { libc::ftruncate(fd, size as i64) };
        let mut buf = vec![0u8; 4 << 20];
        let mut p = 0u64;
        let end = upto.min(size);
        while p < end {
            let n = ((end - p) as usize).min(buf.len());
            pattern_fill(&mut buf[..n], p, seed);
            // SAFETY: buf valid for n bytes.
            let w = unsafe { libc::pwrite(fd, buf.as_ptr() as *const _, n, p as i64) };
            assert_eq!(w, n as isize, "pwrite: {}", io::Error::last_os_error());
            p += n as u64;
        }
        fd
    }

    #[derive(Debug)]
    enum Verdict {
        Ok(u64),
        Zeros(u64),
        Mismatch(u64),
        Err(i32),
    }

    fn verify(buf: &[u8], off: u64, seed: u64) -> Option<(u64, bool)> {
        let mut exp = vec![0u8; buf.len()];
        pattern_fill(&mut exp, off, seed);
        if buf == &exp[..] {
            return None;
        }
        let i = buf
            .iter()
            .zip(exp.iter())
            .position(|(a, b)| a != b)
            .unwrap();
        let s = i & !4095;
        let e = (s + 4096).min(buf.len());
        Some((off + i as u64, buf[s..e].iter().all(|b| *b == 0)))
    }

    fn errno() -> i32 {
        io::Error::last_os_error().raw_os_error().unwrap_or(0)
    }

    fn check(path: &str, off: u64, len: u64, seed: u64, method: &str, bs: usize) -> Verdict {
        let flags = if method == "odirect" {
            libc::O_RDONLY | libc::O_DIRECT
        } else {
            libc::O_RDONLY
        };
        let fd = match open(path, flags) {
            Ok(f) => f,
            Err(e) => return Verdict::Err(e.raw_os_error().unwrap_or(0)),
        };
        // SAFETY: zeroed stat is a valid out-parameter.
        let mut st: libc::stat = unsafe { std::mem::zeroed() };
        unsafe { libc::fstat(fd, &mut st) };
        let size = st.st_size as u64;
        let end = if len == 0 {
            size
        } else {
            (off + len).min(size)
        };
        let r = match method {
            "mmap" => check_mmap(fd, off, end, seed),
            "sendfile" | "cfr" | "splice" => check_pipe(fd, off, end, seed, method, bs),
            _ => check_pread(fd, off, end, seed, bs),
        };
        unsafe { libc::close(fd) };
        r
    }

    fn check_pread(fd: i32, off: u64, end: u64, seed: u64, bs: usize) -> Verdict {
        let layout = std::alloc::Layout::from_size_align(bs, 4096).unwrap();
        // SAFETY: layout is non-zero; freed below.
        let ptr = unsafe { std::alloc::alloc(layout) };
        let buf = unsafe { std::slice::from_raw_parts_mut(ptr, bs) };
        let mut p = off;
        let mut out = None;
        while p < end {
            let n = ((end - p) as usize).min(bs);
            let r = unsafe { libc::pread(fd, buf.as_mut_ptr() as *mut _, n, p as i64) };
            if r < 0 {
                out = Some(Verdict::Err(errno()));
                break;
            }
            if r == 0 {
                break;
            }
            if let Some((at, zero)) = verify(&buf[..r as usize], p, seed) {
                out = Some(if zero {
                    Verdict::Zeros(at)
                } else {
                    Verdict::Mismatch(at)
                });
                break;
            }
            p += r as u64;
        }
        unsafe { std::alloc::dealloc(ptr, layout) };
        out.unwrap_or(Verdict::Ok(p - off))
    }

    fn check_mmap(fd: i32, off: u64, end: u64, seed: u64) -> Verdict {
        let moff = off & !4095;
        let len = (end - moff) as usize;
        // SAFETY: mapping a file we opened; unmapped below.
        let m = unsafe {
            libc::mmap(
                std::ptr::null_mut(),
                len,
                libc::PROT_READ,
                libc::MAP_SHARED,
                fd,
                moff as i64,
            )
        };
        if m == libc::MAP_FAILED {
            return Verdict::Err(errno());
        }
        let s = unsafe { std::slice::from_raw_parts(m as *const u8, len) };
        let mut p = 0usize;
        let mut out = Verdict::Ok(len as u64);
        while p < len {
            let n = (len - p).min(1 << 20);
            if let Some((at, zero)) = verify(&s[p..p + n], moff + p as u64, seed) {
                out = if zero {
                    Verdict::Zeros(at)
                } else {
                    Verdict::Mismatch(at)
                };
                break;
            }
            p += n;
        }
        unsafe { libc::munmap(m, len) };
        out
    }

    fn check_pipe(fd: i32, off: u64, end: u64, seed: u64, method: &str, bs: usize) -> Verdict {
        let len = (end - off) as usize;
        let dir = std::env::var("SMBGUARD_TMPDIR").unwrap_or_else(|_| "/tmp".into());
        let tmp = format!("{dir}/smbguard-tool-{}.out", std::process::id());
        let out = open(&tmp, libc::O_RDWR | libc::O_CREAT | libc::O_TRUNC).unwrap();
        let mut done = 0usize;
        let mut res = None;
        while done < len {
            let n = (len - done).min(bs);
            let mut o = (off + done as u64) as i64;
            // SAFETY: plain syscalls on our descriptors.
            let r = unsafe {
                match method {
                    "sendfile" => libc::sendfile(out, fd, &mut o, n),
                    "cfr" => libc::copy_file_range(fd, &mut o, out, std::ptr::null_mut(), n, 0),
                    _ => {
                        let mut pp = [0i32; 2];
                        libc::pipe(pp.as_mut_ptr());
                        let r =
                            libc::splice(fd, &mut o, pp[1], std::ptr::null_mut(), n.min(65536), 0);
                        if r > 0 {
                            libc::splice(
                                pp[0],
                                std::ptr::null_mut(),
                                out,
                                std::ptr::null_mut(),
                                r as usize,
                                0,
                            );
                        }
                        libc::close(pp[0]);
                        libc::close(pp[1]);
                        r
                    }
                }
            };
            if r < 0 {
                res = Some(Verdict::Err(errno()));
                break;
            }
            if r == 0 {
                break;
            }
            done += r as usize;
        }
        let v = res.unwrap_or_else(|| {
            let mut buf = vec![0u8; done];
            unsafe { libc::pread(out, buf.as_mut_ptr() as *mut _, done, 0) };
            match verify(&buf, off, seed) {
                None => Verdict::Ok(done as u64),
                Some((at, true)) => Verdict::Zeros(at),
                Some((at, false)) => Verdict::Mismatch(at),
            }
        });
        unsafe { libc::close(out) };
        let _ = std::fs::remove_file(&tmp);
        v
    }

    fn arg<'a>(a: &'a [String], name: &str) -> Option<&'a str> {
        a.iter()
            .position(|x| x == name)
            .and_then(|i| a.get(i + 1))
            .map(|s| s.as_str())
    }

    fn num(s: &str) -> u64 {
        let s = s.trim();
        let (n, m) = match s.chars().last() {
            Some('K' | 'k') => (&s[..s.len() - 1], 1u64 << 10),
            Some('M' | 'm') => (&s[..s.len() - 1], 1 << 20),
            Some('G' | 'g') => (&s[..s.len() - 1], 1 << 30),
            _ => (s, 1),
        };
        n.parse::<u64>().expect("number") * m
    }

    fn report(tag: &str, v: &Verdict, t: Duration) -> i32 {
        let s = t.as_secs_f64();
        match v {
            Verdict::Ok(n) => {
                println!("{tag}: OK {n} bytes verified in {s:.3}s");
                0
            }
            Verdict::Zeros(at) => {
                println!("{tag}: ZEROS at offset {at} (after {s:.3}s)  <-- DATA CORRUPTION");
                2
            }
            Verdict::Mismatch(at) => {
                println!("{tag}: MISMATCH at offset {at} (after {s:.3}s)");
                3
            }
            Verdict::Err(e) => {
                println!(
                    "{tag}: ERROR errno={e} ({}) after {s:.3}s",
                    io::Error::from_raw_os_error(*e)
                );
                4
            }
        }
    }

    fn set_marker(fd: i32, m: &Marker) {
        xattr::write(bfd(fd), m).expect("write marker (needs root)");
    }

    pub fn main() {
        let a: Vec<String> = std::env::args().collect();
        let seed = arg(&a, "--seed").map(num).unwrap_or(1);
        let cmd = a.get(1).map(|s| s.as_str()).unwrap_or("");
        let code = match cmd {
            // gen <path> <size> --seed S: an ordinary (unmanaged) pattern file.
            "gen" => {
                let fd = gen_file(&a[2], num(&a[3]), seed, num(&a[3]));
                unsafe { libc::fsync(fd) };
                if a.iter().any(|x| x == "--resident-marker") {
                    set_marker(fd, &Marker::resident(seed, 1, num(&a[3]), 1));
                }
                unsafe { libc::close(fd) };
                0
            }
            // mkstub <path> <size> <resident> --seed S [--online]: a PARTIAL stub
            // whose file_id (and pattern seed) is S, prefix [0, resident) local.
            "mkstub" => {
                let (path, size, resident) = (&a[2], num(&a[3]), num(&a[4]));
                let hole_from = resident.div_ceil(4096) * 4096;
                let fd = gen_file(path, size, seed, hole_from.min(size));
                unsafe { libc::fsync(fd) };
                let flags = if a.iter().any(|x| x == "--online") {
                    Flags::ONLINE
                } else {
                    Flags::ARCHIVED
                };
                let mut m = Marker::partial(seed, 1, size, resident.min(size), 1, flags);
                m.marker_seq = 1;
                set_marker(fd, &m);
                unsafe {
                    libc::fsync(fd);
                    libc::posix_fadvise(fd, 0, 0, libc::POSIX_FADV_DONTNEED);
                    libc::close(fd);
                }
                println!("stub {path}: size={size} resident={resident} file_id={seed}");
                0
            }
            // mkudir <path> --seed <file_id>: an unfilled rewind directory
            // (READONLY | DIR_UNPOPULATED), as the daemon makes them.
            "mkudir" => {
                std::fs::create_dir_all(&a[2]).expect("mkdir");
                let fd = open(&a[2], libc::O_RDONLY | libc::O_DIRECTORY).expect("open dir");
                let mut m = Marker::resident(seed, 0, 0, 1);
                m.flags = Flags::READONLY.with(Flags::DIR_UNPOPULATED);
                m.marker_seq = 1;
                set_marker(fd, &m);
                // SAFETY: our own descriptor, closed once.
                unsafe { libc::close(fd) };
                println!("unfilled directory {}: file_id={seed}", a[2]);
                0
            }
            // dirmarker <path>: a directory's marker, read by path
            // (getxattr opens nothing, so a guarded directory's open
            // mark raises no event).
            "dirmarker" => {
                let mut buf = vec![0u8; 512];
                // SAFETY: NUL-terminated strings, buf valid for its length.
                let n = unsafe {
                    libc::getxattr(
                        cpath(&a[2]).as_ptr(),
                        cpath(omnifile_marker::XATTR_NAME).as_ptr(),
                        buf.as_mut_ptr() as *mut _,
                        buf.len(),
                    )
                };
                if n < 0 {
                    println!("no marker: {}", io::Error::last_os_error());
                } else {
                    match Marker::decode(&buf[..n as usize]) {
                        Ok(m) => println!(
                            "{m:?} unpopulated={}",
                            m.flags.contains(Flags::DIR_UNPOPULATED)
                        ),
                        Err(e) => println!("bad marker: {e}"),
                    }
                }
                0
            }
            // marker <path>: print the marker.
            "marker" => {
                let fd = open(&a[2], libc::O_RDONLY | libc::O_NONBLOCK).expect("open");
                let r = xattr::read(bfd(fd));
                println!("{r:?}");
                unsafe { libc::close(fd) };
                0
            }
            // check <path> [--off O] [--len L] [--method pread|odirect|mmap|sendfile|cfr|splice] [--bs N]
            "check" => {
                let off = arg(&a, "--off").map(num).unwrap_or(0);
                let len = arg(&a, "--len").map(num).unwrap_or(0);
                let method = arg(&a, "--method").unwrap_or("pread");
                let bs = arg(&a, "--bs").map(num).unwrap_or(1 << 20) as usize;
                let t0 = Instant::now();
                let v = check(&a[2], off, len, seed, method, bs);
                report(
                    &format!("check {} [{off},+{len}) via {method}", a[2]),
                    &v,
                    t0.elapsed(),
                )
            }
            // delayed <path> <waitfile> <off> <len>: open now, read once waitfile exists.
            "delayed" => {
                let fd = open(&a[2], libc::O_RDONLY).unwrap();
                println!("opened {} as fd {fd}; waiting for {}", a[2], a[3]);
                while !Path::new(&a[3]).exists() {
                    std::thread::sleep(Duration::from_millis(100));
                }
                let (off, len) = (num(&a[4]), num(&a[5]));
                let t0 = Instant::now();
                let v = check_pread(fd, off, off + len, seed, 1 << 20);
                report("delayed read on pre-opened fd", &v, t0.elapsed())
            }
            // mremap <path> <init_len> <new_len>: the known kernel gap (no event).
            "mremap" => {
                let fd = open(&a[2], libc::O_RDONLY).unwrap();
                let (l0, l1) = (num(&a[3]) as usize, num(&a[4]) as usize);
                let m = unsafe {
                    libc::mmap(
                        std::ptr::null_mut(),
                        l0,
                        libc::PROT_READ,
                        libc::MAP_SHARED,
                        fd,
                        0,
                    )
                };
                assert!(
                    m != libc::MAP_FAILED,
                    "mmap: {}",
                    io::Error::last_os_error()
                );
                let m2 = unsafe { libc::mremap(m, l0, l1, libc::MREMAP_MAYMOVE) };
                if m2 == libc::MAP_FAILED {
                    println!("mremap -> {}", io::Error::last_os_error());
                    4
                } else {
                    let s = unsafe { std::slice::from_raw_parts(m2 as *const u8, l1) };
                    let t0 = Instant::now();
                    let v = match verify(&s[l0..l1], l0 as u64, seed) {
                        None => Verdict::Ok((l1 - l0) as u64),
                        Some((at, true)) => Verdict::Zeros(at),
                        Some((at, false)) => Verdict::Mismatch(at),
                    };
                    report("mremap-grown region", &v, t0.elapsed())
                }
            }
            // owrite <path> <len> --seed S: open(O_WRONLY|O_TRUNC) and write the
            // pattern for S over [0, len) (an overwrite that raises no
            // pre-content event for the truncation).
            // pwrite <path> <off> <len> --seed S: write the pattern for S over
            // [off, off+len) through an ordinary O_WRONLY open (no truncation).
            "pwrite" => {
                let (off, len) = (num(&a[3]), num(&a[4]) as usize);
                let fd = open(&a[2], libc::O_WRONLY).expect("open");
                let mut buf = vec![0u8; len];
                pattern_fill(&mut buf, off, seed);
                let w = unsafe { libc::pwrite(fd, buf.as_ptr() as *const _, len, off as i64) };
                let e = errno();
                unsafe { libc::close(fd) };
                if w == len as isize {
                    println!("pwrite {}: {len} bytes at {off}", a[2]);
                    0
                } else {
                    println!("pwrite {}: ERROR {}", a[2], io::Error::from_raw_os_error(e));
                    4
                }
            }
            // parwrite <path> <len1> <off2> <len2> --seed S [--gap-ms D]: one
            // open(O_WRONLY|O_TRUNC), then two writers on it, like smbd's aio
            // threads: [0,len1) at once, [off2,off2+len2) D ms later. Both
            // ranges get the pattern for S.
            "parwrite" => {
                let (l1, o2, l2) = (num(&a[3]) as usize, num(&a[4]), num(&a[5]) as usize);
                let gap = arg(&a, "--gap-ms").map(num).unwrap_or(500);
                let fd = open(&a[2], libc::O_WRONLY | libc::O_TRUNC).expect("open O_TRUNC");
                let w = |off: u64, len: usize| {
                    std::thread::spawn(move || {
                        let mut buf = vec![0u8; len];
                        pattern_fill(&mut buf, off, seed);
                        let r =
                            unsafe { libc::pwrite(fd, buf.as_ptr() as *const _, len, off as i64) };
                        (r == len as isize, errno())
                    })
                };
                let t1 = w(0, l1);
                std::thread::sleep(Duration::from_millis(gap));
                let t2 = w(o2, l2);
                let (r1, r2) = (t1.join().unwrap(), t2.join().unwrap());
                unsafe { libc::close(fd) };
                println!(
                    "parwrite {}: writer1 ok={} writer2 ok={} (errno {} {})",
                    a[2], r1.0, r2.0, r1.1, r2.1
                );
                if r1.0 && r2.0 { 0 } else { 4 }
            }
            "owrite" => {
                let len = num(&a[3]);
                let fd = open(&a[2], libc::O_WRONLY | libc::O_TRUNC).expect("open O_TRUNC");
                let mut buf = vec![0u8; len as usize];
                pattern_fill(&mut buf, 0, seed);
                let w = unsafe { libc::pwrite(fd, buf.as_ptr() as *const _, buf.len(), 0) };
                let e = errno();
                unsafe { libc::close(fd) };
                if w == len as isize {
                    println!("owrite {}: {len} bytes", a[2]);
                    0
                } else {
                    println!("owrite {}: ERROR {}", a[2], io::Error::from_raw_os_error(e));
                    4
                }
            }
            // flood <path> <nthreads> <stride> <start> [--timeout S]
            "flood" => flood(&a, seed),
            // kernel-check <dir>: this kernel delivers fanotify pre-content
            // events and honours their answer (the appliance's boot gate,
            // appliance/README.md): a read of a marked file is held, and
            // denied with EIO. <dir> must be on xfs, ext4 or btrfs.
            "kernel-check" => kernel_check(&a[2]),
            _ => {
                eprintln!(
                    "usage: smbguard-tool gen|mkstub|mkudir|marker|dirmarker|check|delayed|mremap|owrite|pwrite|parwrite|flood|kernel-check ..."
                );
                1
            }
        };
        std::process::exit(code);
    }

    fn flood(a: &[String], seed: u64) -> i32 {
        let path = a[2].clone();
        let n = num(&a[3]) as usize;
        let stride = num(&a[4]);
        let start = num(&a[5]);
        let timeout = arg(a, "--timeout").map(num).unwrap_or(60);
        // One descriptor per reader: lift our own limit first.
        let mut rl = libc::rlimit {
            rlim_cur: 0,
            rlim_max: 0,
        };
        // SAFETY: rl is a valid in/out parameter.
        unsafe {
            libc::getrlimit(libc::RLIMIT_NOFILE, &mut rl);
            rl.rlim_cur = rl.rlim_max;
            libc::setrlimit(libc::RLIMIT_NOFILE, &rl);
        }
        let ok = Arc::new(AtomicU64::new(0));
        let zeros = Arc::new(AtomicU64::new(0));
        let errs = Arc::new(AtomicU64::new(0));
        let started = Arc::new(AtomicU64::new(0));
        let t0 = Instant::now();
        for i in 0..n {
            let (ok, zeros, errs, started, path) = (
                ok.clone(),
                zeros.clone(),
                errs.clone(),
                started.clone(),
                path.clone(),
            );
            let off = start + i as u64 * stride;
            let r = std::thread::Builder::new()
                .stack_size(64 << 10)
                .spawn(move || {
                    started.fetch_add(1, Ordering::Relaxed);
                    let Ok(fd) = open(&path, libc::O_RDONLY) else {
                        errs.fetch_add(1, Ordering::Relaxed);
                        return;
                    };
                    let mut buf = [0u8; 4096];
                    let r =
                        unsafe { libc::pread(fd, buf.as_mut_ptr() as *mut _, 4096, off as i64) };
                    if r < 0 {
                        errs.fetch_add(1, Ordering::Relaxed);
                    } else if verify(&buf[..r as usize], off, seed).is_some() {
                        zeros.fetch_add(1, Ordering::Relaxed);
                    } else {
                        ok.fetch_add(1, Ordering::Relaxed);
                    }
                    unsafe { libc::close(fd) };
                });
            if let Err(e) = r {
                println!("thread spawn {i} failed: {e}");
                break;
            }
        }
        loop {
            let (o, z, e, s) = (
                ok.load(Ordering::Relaxed),
                zeros.load(Ordering::Relaxed),
                errs.load(Ordering::Relaxed),
                started.load(Ordering::Relaxed),
            );
            println!(
                "t={:.1}s started={s} completed_ok={o} completed_ZEROS={z} errors={e} still_blocked={}",
                t0.elapsed().as_secs_f64(),
                s - o - z - e
            );
            if o + z + e >= n as u64 || t0.elapsed().as_secs() >= timeout {
                break;
            }
            std::thread::sleep(Duration::from_secs(2));
        }
        if zeros.load(Ordering::Relaxed) > 0 {
            2
        } else {
            0
        }
    }
}
