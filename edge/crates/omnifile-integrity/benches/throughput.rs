//! Throughput benchmark: `cargo bench -p omnifile-integrity`.
//!
//! Env: `BENCH_MIB` (in-memory buffer, default 512), `BENCH_FILE_MIB` (temp
//! file for the end-to-end run, default 2048; 0 skips it).

use std::hint::black_box;
use std::io::{Cursor, Read, Write};
use std::time::{Duration, Instant};

use omnifile_integrity::crc64::{combine, combine_all};
use omnifile_integrity::{Options, crc64_nvme, hash_file, hash_reader};

fn env_mib(name: &str, default: u64) -> u64 {
    std::env::var(name)
        .ok()
        .and_then(|v| v.parse().ok())
        .unwrap_or(default)
}

/// Best of `reps` runs, as MB/s (10^6 bytes per second).
fn mbps(bytes: u64, reps: u32, mut f: impl FnMut()) -> f64 {
    f(); // warm up
    let mut best = Duration::MAX;
    for _ in 0..reps {
        let t = Instant::now();
        f();
        best = best.min(t.elapsed());
    }
    bytes as f64 / best.as_secs_f64() / 1e6
}

fn report(name: &str, v: f64) {
    println!("{name:<52} {v:>10.0} MB/s");
}

fn main() {
    // `cargo bench` passes `--bench`; ignore arguments.
    let mib = env_mib("BENCH_MIB", 512);
    let n = (mib << 20) as usize;
    let mut data = vec![0u8; n];
    let mut x = 0x9e37_79b9_7f4a_7c15u64;
    for c in data.chunks_mut(8) {
        x ^= x << 13;
        x ^= x >> 7;
        x ^= x << 17;
        c.copy_from_slice(&x.to_le_bytes()[..c.len()]);
    }
    let bytes = n as u64;
    println!(
        "buffer {mib} MiB, {} rayon threads",
        rayon::current_num_threads()
    );

    report(
        "crc64-nvme crc-fast (1 thread)",
        mbps(bytes, 5, || {
            black_box(crc64_nvme(black_box(&data)));
        }),
    );
    let table = crc::Crc::<u64>::new(&crc::CRC_64_NVME);
    let small = &data[..(64 << 20).min(n)];
    report(
        "crc64-nvme `crc` crate table (1 thread, 64 MiB)",
        mbps(small.len() as u64, 3, || {
            black_box(table.checksum(black_box(small)));
        }),
    );
    report(
        "blake3 update (1 thread)",
        mbps(bytes, 5, || {
            let mut h = blake3::Hasher::new();
            h.update(black_box(&data));
            black_box(h.finalize());
        }),
    );
    report(
        "blake3 update_rayon",
        mbps(bytes, 5, || {
            let mut h = blake3::Hasher::new();
            h.update_rayon(black_box(&data));
            black_box(h.finalize());
        }),
    );

    // Combine cost.
    let t = Instant::now();
    let iters = 100_000u64;
    let mut acc = 0u64;
    for i in 0..iters {
        acc = combine(acc, i, black_box(64 << 20));
    }
    black_box(acc);
    println!(
        "{:<52} {:>10.0} ns",
        "combine(.., len = 64 MiB), per call",
        t.elapsed().as_nanos() as f64 / iters as f64
    );
    let t = Instant::now();
    let blocks: Vec<(u64, u64)> = (0..50_000u64).map(|i| (i, 4_000 << 20)).collect();
    black_box(combine_all(black_box(blocks)));
    println!(
        "{:<52} {:>10.2} ms",
        "combine_all of 50,000 blocks",
        t.elapsed().as_secs_f64() * 1e3
    );

    // Single pass from memory (CPU bound).
    for (chunk_mib, parallel) in [(4, true), (8, true), (16, true), (8, false)] {
        let opts = Options {
            chunk_size: chunk_mib << 20,
            queue_depth: 4,
            parallel,
        };
        report(
            &format!("single pass from memory, {chunk_mib} MiB chunks, parallel={parallel}"),
            mbps(bytes, 3, || {
                black_box(hash_reader(Cursor::new(&data), bytes, opts).unwrap());
            }),
        );
    }

    // Single pass from a file (page cache warm after the write: an upper bound
    // on what the pipeline can sustain, compared with a plain read).
    let file_mib = env_mib("BENCH_FILE_MIB", 2048);
    if file_mib == 0 {
        return;
    }
    let path =
        std::env::temp_dir().join(format!("omnifile-integrity-bench-{}", std::process::id()));
    {
        let mut f = std::fs::File::create(&path).unwrap();
        let mut left = file_mib << 20;
        while left > 0 {
            let k = left.min(bytes) as usize;
            f.write_all(&data[..k]).unwrap();
            left -= k as u64;
        }
        f.sync_all().unwrap();
    }
    let fbytes = file_mib << 20;
    println!("file {file_mib} MiB at {}", path.display());
    report(
        "plain sequential read, 8 MiB buffer",
        mbps(fbytes, 3, || {
            let mut f = std::fs::File::open(&path).unwrap();
            let mut buf = vec![0u8; 8 << 20];
            while f.read(&mut buf).unwrap() > 0 {}
        }),
    );
    report(
        "hash_file (default options)",
        mbps(fbytes, 3, || {
            black_box(hash_file(&path, Options::default()).unwrap());
        }),
    );
    report(
        "hash_file, parallel=false",
        mbps(fbytes, 3, || {
            let opts = Options {
                parallel: false,
                ..Options::default()
            };
            black_box(hash_file(&path, opts).unwrap());
        }),
    );
    std::fs::remove_file(&path).unwrap();
}
