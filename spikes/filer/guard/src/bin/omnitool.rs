//! omnitool: test helpers for the guard spike (pattern files, stubs, verified reads, floods).
//!
//! Pattern: the little-endian u64 at every 8-byte-aligned offset o is o ^ "OMNIFILE" ^ seed, so
//! the data is non-zero and position dependent; a zero page or a misplaced block is detected.

use std::ffi::CString;
use std::io;
use std::os::unix::ffi::OsStrExt;
use std::path::Path;
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::Arc;
use std::time::{Duration, Instant};

const PAT: u64 = 0x4F4D_4E49_4649_4C45;

fn pattern_fill(buf: &mut [u8], off: u64, seed: u64) {
    let base = off & !7;
    let skip = (off - base) as usize;
    if skip == 0 {
        for (i, ch) in buf.chunks_mut(8).enumerate() {
            let w = ((off + 8 * i as u64) ^ PAT ^ seed).to_le_bytes();
            ch.copy_from_slice(&w[..ch.len()]);
        }
    } else {
        let mut tmp = vec![0u8; buf.len() + 8];
        pattern_fill(&mut tmp, base, seed);
        buf.copy_from_slice(&tmp[skip..skip + buf.len()]);
    }
}

fn cpath(p: &str) -> CString {
    CString::new(Path::new(p).as_os_str().as_bytes()).unwrap()
}

fn open(p: &str, flags: i32) -> io::Result<i32> {
    let fd = unsafe { libc::open(cpath(p).as_ptr(), flags | libc::O_CLOEXEC, 0o644) };
    if fd < 0 { Err(io::Error::last_os_error()) } else { Ok(fd) }
}

fn setx(fd: i32, k: &str, v: &str) {
    let k = CString::new(k).unwrap();
    let r = unsafe { libc::fsetxattr(fd, k.as_ptr(), v.as_ptr() as *const _, v.len(), 0) };
    assert!(r == 0, "fsetxattr: {}", io::Error::last_os_error());
}

fn gen_file(path: &str, size: u64, seed: u64, upto: u64) -> i32 {
    let fd = open(path, libc::O_RDWR | libc::O_CREAT | libc::O_TRUNC).expect("open");
    unsafe { libc::ftruncate(fd, size as i64) };
    let mut buf = vec![0u8; 4 << 20];
    let mut p = 0u64;
    while p < upto.min(size) {
        let n = ((upto.min(size) - p) as usize).min(buf.len());
        pattern_fill(&mut buf[..n], p, seed);
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
    for i in 0..buf.len() {
        if buf[i] != exp[i] {
            // is the surrounding 4 KiB zero?
            let s = i & !4095;
            let e = (s + 4096).min(buf.len());
            let zero = buf[s..e].iter().all(|b| *b == 0);
            return Some((off + i as u64, zero));
        }
    }
    None
}

fn check(path: &str, off: u64, len: u64, seed: u64, method: &str, bs: usize) -> Verdict {
    let flags = if method == "odirect" { libc::O_RDONLY | libc::O_DIRECT } else { libc::O_RDONLY };
    let fd = match open(path, flags) { Ok(f) => f, Err(e) => return Verdict::Err(e.raw_os_error().unwrap_or(0)) };
    let mut st: libc::stat = unsafe { std::mem::zeroed() };
    unsafe { libc::fstat(fd, &mut st) };
    let size = st.st_size as u64;
    let end = if len == 0 { size } else { (off + len).min(size) };
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
    let ptr = unsafe { std::alloc::alloc(layout) };
    let buf = unsafe { std::slice::from_raw_parts_mut(ptr, bs) };
    let mut p = off;
    while p < end {
        let n = ((end - p) as usize).min(bs);
        let r = unsafe { libc::pread(fd, buf.as_mut_ptr() as *mut _, n, p as i64) };
        if r < 0 {
            return Verdict::Err(io::Error::last_os_error().raw_os_error().unwrap_or(0));
        }
        if r == 0 {
            break;
        }
        if let Some((at, zero)) = verify(&buf[..r as usize], p, seed) {
            return if zero { Verdict::Zeros(at) } else { Verdict::Mismatch(at) };
        }
        p += r as u64;
    }
    Verdict::Ok(p - off)
}

fn check_mmap(fd: i32, off: u64, end: u64, seed: u64) -> Verdict {
    let moff = off & !4095;
    let len = (end - moff) as usize;
    let m = unsafe { libc::mmap(std::ptr::null_mut(), len, libc::PROT_READ, libc::MAP_SHARED, fd, moff as i64) };
    if m == libc::MAP_FAILED {
        return Verdict::Err(io::Error::last_os_error().raw_os_error().unwrap_or(0));
    }
    eprintln!("mmap({len} bytes at {moff}) returned");
    let s = unsafe { std::slice::from_raw_parts(m as *const u8, len) };
    let mut p = 0usize;
    while p < len {
        let n = (len - p).min(1 << 20);
        if let Some((at, zero)) = verify(&s[p..p + n], moff + p as u64, seed) {
            unsafe { libc::munmap(m, len) };
            return if zero { Verdict::Zeros(at) } else { Verdict::Mismatch(at) };
        }
        p += n;
    }
    unsafe { libc::munmap(m, len) };
    Verdict::Ok(len as u64)
}

/// sendfile / copy_file_range / splice into a temp file or pipe, then verify.
fn check_pipe(fd: i32, off: u64, end: u64, seed: u64, method: &str, bs: usize) -> Verdict {
    let len = (end - off) as usize;
    // same filesystem as the source for copy_file_range (cross-fs gives EXDEV)
    let tmp = match std::env::var("OMNITOOL_TMPDIR") {
        Ok(d) => format!("{d}/omnitool-{}.out", std::process::id()),
        Err(_) => format!("/tmp/omnitool-{}.out", std::process::id()),
    };
    let out = open(&tmp, libc::O_RDWR | libc::O_CREAT | libc::O_TRUNC).unwrap();
    let mut done = 0usize;
    while done < len {
        let n = (len - done).min(bs);
        let mut o = (off + done as u64) as i64;
        let r = match method {
            "sendfile" => unsafe { libc::sendfile(out, fd, &mut o, n) },
            "cfr" => unsafe { libc::copy_file_range(fd, &mut o, out, std::ptr::null_mut(), n, 0) },
            _ => {
                let mut pp = [0i32; 2];
                unsafe { libc::pipe(pp.as_mut_ptr()) };
                let r = unsafe { libc::splice(fd, &mut o, pp[1], std::ptr::null_mut(), n.min(65536), 0) };
                if r > 0 {
                    unsafe { libc::splice(pp[0], std::ptr::null_mut(), out, std::ptr::null_mut(), r as usize, 0) };
                }
                unsafe { libc::close(pp[0]); libc::close(pp[1]) };
                r
            }
        };
        if r < 0 {
            let e = io::Error::last_os_error().raw_os_error().unwrap_or(0);
            unsafe { libc::close(out) };
            let _ = std::fs::remove_file(&tmp);
            return Verdict::Err(e);
        }
        if r == 0 { break; }
        done += r as usize;
    }
    let mut buf = vec![0u8; done];
    unsafe { libc::pread(out, buf.as_mut_ptr() as *mut _, done, 0) };
    unsafe { libc::close(out) };
    let _ = std::fs::remove_file(&tmp);
    match verify(&buf, off, seed) {
        None => Verdict::Ok(done as u64),
        Some((at, true)) => Verdict::Zeros(at),
        Some((at, false)) => Verdict::Mismatch(at),
    }
}

fn arg<'a>(a: &'a [String], name: &str) -> Option<&'a str> {
    a.iter().position(|x| x == name).and_then(|i| a.get(i + 1)).map(|s| s.as_str())
}
fn num(s: &str) -> u64 {
    let s = s.trim();
    let (n, m) = match s.chars().last().unwrap() {
        'K' | 'k' => (&s[..s.len() - 1], 1u64 << 10),
        'M' | 'm' => (&s[..s.len() - 1], 1 << 20),
        'G' | 'g' => (&s[..s.len() - 1], 1 << 30),
        _ => (s, 1),
    };
    n.parse::<u64>().unwrap() * m
}

fn report(tag: &str, v: &Verdict, t: Duration) -> i32 {
    match v {
        Verdict::Ok(n) => { println!("{tag}: OK {n} bytes verified in {:.3}s", t.as_secs_f64()); 0 }
        Verdict::Zeros(at) => { println!("{tag}: ZEROS at offset {at} (after {:.3}s)  <-- DATA CORRUPTION", t.as_secs_f64()); 2 }
        Verdict::Mismatch(at) => { println!("{tag}: MISMATCH at offset {at} (after {:.3}s)", t.as_secs_f64()); 3 }
        Verdict::Err(e) => { println!("{tag}: ERROR errno={e} ({}) after {:.3}s", io::Error::from_raw_os_error(*e), t.as_secs_f64()); 4 }
    }
}

fn main() {
    let a: Vec<String> = std::env::args().collect();
    let seed = arg(&a, "--seed").map(num).unwrap_or(0);
    let cmd = a.get(1).map(|s| s.as_str()).unwrap_or("");
    let code = match cmd {
        "gen" => {
            let fd = gen_file(&a[2], num(&a[3]), seed, num(&a[3]));
            unsafe { libc::fsync(fd); libc::close(fd) };
            0
        }
        // mkstub <path> <size> <resident> <key> [--synthetic] [--backend DIR]
        "mkstub" => {
            let (path, size, resident, key) = (&a[2], num(&a[3]), num(&a[4]), a[5].clone());
            let synthetic = a.iter().any(|x| x == "--synthetic");
            let backend = arg(&a, "--backend").unwrap_or("/srv/omni-a/backend");
            let key = if synthetic { format!("synthetic:{seed}") } else { key };
            let fd = if synthetic {
                gen_file(path, size, seed, resident)
            } else {
                let bfd = gen_file(&format!("{backend}/{key}"), size, seed, size);
                unsafe { libc::fsync(bfd); libc::close(bfd) };
                // the local copy: full data then punch (as the product would after upload)
                gen_file(path, size, seed, size)
            };
            unsafe { libc::fsync(fd) };
            let hole_from = resident.div_ceil(4096) * 4096;
            if hole_from < size {
                let r = unsafe { libc::fallocate(fd, libc::FALLOC_FL_PUNCH_HOLE | libc::FALLOC_FL_KEEP_SIZE, hole_from as i64, (size - hole_from) as i64) };
                assert!(r == 0, "punch: {}", io::Error::last_os_error());
            }
            setx(fd, "user.omnifile.key", &key);
            setx(fd, "user.omnifile.resident_bytes", &resident.to_string());
            setx(fd, "user.omnifile.state", "stub");
            unsafe { libc::fsync(fd); libc::close(fd) };
            // drop page cache so later reads go to disk (holes)
            let fd = open(path, libc::O_RDONLY).unwrap();
            unsafe { libc::posix_fadvise(fd, 0, 0, libc::POSIX_FADV_DONTNEED); libc::close(fd) };
            println!("stub {path}: size={size} resident={resident} key={key}");
            0
        }
        // restub <path> <resident>: set stub xattrs, then punch the body (re-tiering a resident file)
        "restub" => {
            let fd = open(&a[2], libc::O_RDWR).unwrap();
            let resident = num(&a[3]);
            let mut st: libc::stat = unsafe { std::mem::zeroed() };
            unsafe { libc::fstat(fd, &mut st) };
            setx(fd, "user.omnifile.resident_bytes", &resident.to_string());
            setx(fd, "user.omnifile.state", "stub");
            let from = resident.div_ceil(4096) * 4096;
            let r = unsafe { libc::fallocate(fd, libc::FALLOC_FL_PUNCH_HOLE | libc::FALLOC_FL_KEEP_SIZE, from as i64, st.st_size - from as i64) };
            println!("restub {}: punch rc={r} {}", a[2], if r < 0 { io::Error::last_os_error().to_string() } else { String::new() });
            unsafe { libc::close(fd) };
            0
        }
        // check <path> [--off O] [--len L] [--method m] [--bs N]
        "check" => {
            let off = arg(&a, "--off").map(num).unwrap_or(0);
            let len = arg(&a, "--len").map(num).unwrap_or(0);
            let method = arg(&a, "--method").unwrap_or("pread");
            let bs = arg(&a, "--bs").map(num).unwrap_or(1 << 20) as usize;
            let t0 = Instant::now();
            let v = check(&a[2], off, len, seed, method, bs);
            report(&format!("check {} [{off},+{len}) via {method}", a[2]), &v, t0.elapsed())
        }
        // delayed <path> <waitfile> <off> <len>: open now, read after waitfile appears (A9)
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
        // mremap <path> <init_len> <new_len>: map resident head, grow the mapping, touch the rest
        "mremap" => {
            let fd = open(&a[2], libc::O_RDONLY).unwrap();
            let (l0, l1) = (num(&a[3]) as usize, num(&a[4]) as usize);
            let m = unsafe { libc::mmap(std::ptr::null_mut(), l0, libc::PROT_READ, libc::MAP_SHARED, fd, 0) };
            assert!(m != libc::MAP_FAILED, "mmap: {}", io::Error::last_os_error());
            println!("mmap({l0}) ok");
            let m2 = unsafe { libc::mremap(m, l0, l1, libc::MREMAP_MAYMOVE) };
            if m2 == libc::MAP_FAILED {
                println!("mremap -> {}", io::Error::last_os_error());
                1
            } else {
                println!("mremap({l0} -> {l1}) ok, touching [{l0},{l1})");
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
        // flood <path> <nthreads> <stride> <start> [--timeout S] [--report S]
        "flood" => {
            let path = a[2].clone();
            let n = num(&a[3]) as usize;
            let stride = num(&a[4]);
            let start = num(&a[5]);
            let timeout = arg(&a, "--timeout").map(num).unwrap_or(60);
            let ok = Arc::new(AtomicU64::new(0));
            let zeros = Arc::new(AtomicU64::new(0));
            let errs = Arc::new(AtomicU64::new(0));
            let started = Arc::new(AtomicU64::new(0));
            let t0 = Instant::now();
            for i in 0..n {
                let (ok, zeros, errs, started, path) = (ok.clone(), zeros.clone(), errs.clone(), started.clone(), path.clone());
                let off = start + i as u64 * stride;
                let r = std::thread::Builder::new().stack_size(64 << 10).spawn(move || {
                    let fd = open(&path, libc::O_RDONLY).unwrap();
                    started.fetch_add(1, Ordering::Relaxed);
                    let mut buf = [0u8; 4096];
                    let r = unsafe { libc::pread(fd, buf.as_mut_ptr() as *mut _, 4096, off as i64) };
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
                let (o, z, e, s) = (ok.load(Ordering::Relaxed), zeros.load(Ordering::Relaxed), errs.load(Ordering::Relaxed), started.load(Ordering::Relaxed));
                println!("t={:.1}s started={s} completed_ok={o} completed_ZEROS={z} errors={e} still_blocked={}", t0.elapsed().as_secs_f64(), s - o - z - e);
                if o + z + e >= n as u64 || t0.elapsed().as_secs() >= timeout {
                    break;
                }
                std::thread::sleep(Duration::from_secs(2));
            }
            let z = zeros.load(Ordering::Relaxed);
            if z > 0 { 2 } else { 0 }
        }
        _ => {
            eprintln!("usage: omnitool gen|mkstub|check|delayed|mremap|flood ...");
            1
        }
    };
    std::process::exit(code);
}
