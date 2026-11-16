use std::io::{Cursor, Read, Write};

use omnifile_integrity::blocks::{
    BLOCK_ALIGN, BLOCK_ID_LEN, BLOCK_ID_RAW_LEN, MAX_BLOB_SIZE, MAX_BLOCK_SIZE, MAX_BLOCKS, MIB,
    MIN_BLOCK_SIZE,
};
use omnifile_integrity::crc64::{CHECK, Crc64Combiner, combine_op, combine_operator};
use omnifile_integrity::headers::azure_to_s3;
use omnifile_integrity::*;

// --- helpers ---------------------------------------------------------------

/// splitmix64: deterministic test data without a `rand` dependency.
struct Rng(u64);

impl Rng {
    fn next(&mut self) -> u64 {
        self.0 = self.0.wrapping_add(0x9e37_79b9_7f4a_7c15);
        let mut z = self.0;
        z = (z ^ (z >> 30)).wrapping_mul(0xbf58_476d_1ce4_e5b9);
        z = (z ^ (z >> 27)).wrapping_mul(0x94d0_49bb_1331_11eb);
        z ^ (z >> 31)
    }
    fn below(&mut self, n: u64) -> u64 {
        if n == 0 { 0 } else { self.next() % n }
    }
    fn bytes(&mut self, n: usize) -> Vec<u8> {
        let mut v = Vec::with_capacity(n + 8);
        while v.len() < n {
            v.extend_from_slice(&self.next().to_le_bytes());
        }
        v.truncate(n);
        v
    }
}

/// Independent bitwise/table CRC-64/NVME from the `crc` crate's catalogue.
fn reference_crc(data: &[u8]) -> u64 {
    crc::Crc::<u64>::new(&crc::CRC_64_NVME).checksum(data)
}

const SIZES: &[usize] = &[
    0, 1, 2, 3, 7, 8, 15, 16, 17, 31, 32, 63, 64, 65, 127, 128, 129, 255, 256, 257, 511, 512, 1000,
    1023, 1024, 4095, 4096, 4097, 65_535, 65_536, 65_537, 1_000_003,
];

// --- CRC-64/NVME known answers ---------------------------------------------

#[test]
fn check_value_123456789() {
    assert_eq!(CHECK, 0xAE8B_1486_0A79_9888);
    assert_eq!(crc64_nvme(b"123456789"), 0xAE8B_1486_0A79_9888);
    assert_eq!(reference_crc(b"123456789"), 0xAE8B_1486_0A79_9888);
    let mut c = Crc64::new();
    c.update(b"1234");
    c.update(b"56789");
    assert_eq!(c.finalize(), CHECK);
    assert_eq!(c.len(), 9);
}

#[test]
fn empty_is_zero() {
    assert_eq!(crc64_nvme(&[]), 0);
    assert_eq!(Crc64::new().finalize(), 0);
}

#[test]
fn simd_matches_reference_implementation() {
    let mut rng = Rng(1);
    for &n in SIZES {
        let data = rng.bytes(n);
        assert_eq!(crc64_nvme(&data), reference_crc(&data), "len {n}");
    }
    // Unaligned starts exercise the SIMD prologue.
    let data = rng.bytes(10_000);
    for off in 0..32 {
        assert_eq!(crc64_nvme(&data[off..]), reference_crc(&data[off..]));
    }
}

/// Microsoft's structured-body example: CRC of 0x11, of 0x22, and of both,
/// as 8 bytes in wire order. Confirms the algorithm, the little-endian byte
/// order, and combine against a published vector.
#[test]
fn azure_structured_body_vectors() {
    let c11 = crc64_nvme(&[0x11]);
    let c22 = crc64_nvme(&[0x22]);
    let c1122 = crc64_nvme(&[0x11, 0x22]);
    assert_eq!(
        c11.to_le_bytes(),
        [0xd0, 0x61, 0x67, 0x57, 0xb4, 0x5f, 0x54, 0xd2]
    );
    assert_eq!(
        c22.to_le_bytes(),
        [0xd8, 0x4a, 0xfb, 0x9e, 0xa0, 0x4f, 0xc6, 0xda]
    );
    assert_eq!(
        c1122.to_le_bytes(),
        [0xe2, 0xa6, 0x37, 0x74, 0x50, 0xad, 0xc2, 0xef]
    );
    assert_eq!(combine(c11, c22, 1), c1122);
    assert_eq!(azure_content_crc64(c11), "0GFnV7RfVNI=");
    assert_eq!(azure_content_crc64(c22), "2Er7nqBPxto=");
    assert_eq!(azure_content_crc64(c1122), "4qY3dFCtwu8=");
}

// --- combine ---------------------------------------------------------------

#[test]
fn combine_equals_direct_for_every_split() {
    let mut rng = Rng(2);
    for &n in SIZES.iter().filter(|&&n| n <= 4097) {
        let data = rng.bytes(n);
        let whole = crc64_nvme(&data);
        for cut in 0..=n {
            let (a, b) = data.split_at(cut);
            assert_eq!(
                combine(crc64_nvme(a), crc64_nvme(b), b.len() as u64),
                whole,
                "len {n} cut {cut}"
            );
        }
    }
}

#[test]
fn combine_matches_crc_fast_combine() {
    let mut rng = Rng(3);
    for _ in 0..2_000 {
        let a = rng.next();
        let b = rng.next();
        let len = rng.below(1 << 40);
        assert_eq!(
            combine(a, b, len),
            crc_fast::checksum_combine(crc_fast::CrcAlgorithm::Crc64Nvme, a, b, len),
            "len {len}"
        );
    }
}

#[test]
fn combine_all_random_pieces_including_empty_and_ragged_tails() {
    let mut rng = Rng(4);
    for round in 0..200 {
        let n = rng.below(200_000) as usize;
        let data = rng.bytes(n);
        let mut pieces = Vec::new();
        let mut pos = 0;
        while pos < n {
            // Mix empty, tiny and large pieces.
            let len = match rng.below(4) {
                0 => 0,
                1 => rng.below(8) as usize,
                _ => rng.below(40_000) as usize,
            }
            .min(n - pos);
            pieces.push((crc64_nvme(&data[pos..pos + len]), len as u64));
            pos += len;
        }
        assert_eq!(combine_all(pieces), crc64_nvme(&data), "round {round}");
    }
    assert_eq!(combine_all(std::iter::empty()), 0);
}

#[test]
fn combine_equal_blocks_with_cached_operator() {
    let mut rng = Rng(5);
    let block = 4096 + 7;
    let data = rng.bytes(block * 9 + 123);
    let op = combine_operator(block as u64);
    let mut acc = 0u64;
    let mut comb = Crc64Combiner::new();
    for chunk in data.chunks(block) {
        let c = crc64_nvme(chunk);
        acc = if chunk.len() == block {
            combine_op(acc, c, op)
        } else {
            combine(acc, c, chunk.len() as u64)
        };
        comb.push(c, chunk.len() as u64);
    }
    assert_eq!(acc, crc64_nvme(&data));
    assert_eq!(comb.crc(), crc64_nvme(&data));
    assert_eq!(comb.len(), data.len() as u64);
}

#[test]
fn combine_huge_lengths_is_consistent() {
    // Associativity at blob-scale lengths: (seed ‖ 0^b) ‖ 0^a == seed ‖ (0^b ‖ 0^a).
    let c = crc64_nvme(b"seed");
    let a = 190 * (1u64 << 40);
    let b = 12_345;
    let zeros_b = crc64_nvme(&vec![0u8; b as usize]);
    // crc(seed || 0^b) via combine, then extend by a: two orders must match.
    let left = combine(combine(c, zeros_b, b), crc_of_zeros(a), a);
    let right = combine(c, combine(zeros_b, crc_of_zeros(a), a), a + b);
    assert_eq!(left, right);
}

/// CRC of `len` zero bytes, built by doubling with combine.
fn crc_of_zeros(len: u64) -> u64 {
    let mut result = 0u64; // crc of empty
    let mut result_len = 0u64;
    let mut pow = crc64_nvme(&[0]);
    let mut pow_len = 1u64;
    let mut n = len;
    while n > 0 {
        if n & 1 == 1 {
            result = combine(result, pow, pow_len);
            result_len += pow_len;
        }
        pow = combine(pow, pow, pow_len);
        pow_len *= 2;
        n >>= 1;
    }
    assert_eq!(result_len, len);
    result
}

#[test]
fn crc_of_zeros_helper_is_right() {
    for len in [0u64, 1, 2, 3, 100, 4096, 70_001] {
        assert_eq!(crc_of_zeros(len), crc64_nvme(&vec![0u8; len as usize]));
    }
}

// --- block planning --------------------------------------------------------

fn check_plan_invariants(plan: &BlockPlan) {
    let size = plan.size();
    assert!(plan.block_size() >= MIN_BLOCK_SIZE);
    assert!(plan.block_size() <= MAX_BLOCK_SIZE);
    assert_eq!(plan.block_size() % BLOCK_ALIGN, 0);
    assert!(u64::from(plan.count()) <= MAX_BLOCKS);
    assert_eq!(u64::from(plan.count()), size.div_ceil(plan.block_size()));
    // The size rule: never a smaller block than needed, never more than 4 MiB slack over the rule.
    let rule = size.div_ceil(MAX_BLOCKS).max(MIN_BLOCK_SIZE);
    assert!(plan.block_size() >= rule && plan.block_size() < rule + BLOCK_ALIGN);
    if plan.count() > 0 {
        let first = plan.block(0).unwrap();
        let last = plan.block(plan.count() - 1).unwrap();
        assert_eq!(first.offset, 0);
        assert_eq!(last.range().end, size);
        assert!(last.len >= 1 && last.len <= plan.block_size());
    }
    assert!(plan.block(plan.count()).is_none());
}

#[test]
fn plan_zero_bytes() {
    let p = BlockPlan::new(0).unwrap();
    assert_eq!(p.count(), 0);
    assert!(p.is_empty());
    assert_eq!(p.block_size(), 64 * MIB);
    assert_eq!(p.blocks().count(), 0);
    assert!(p.block_at(0).is_none());
    check_plan_invariants(&p);
}

#[test]
fn plan_one_byte() {
    let p = BlockPlan::new(1).unwrap();
    assert_eq!(p.count(), 1);
    assert_eq!(
        p.block(0).unwrap(),
        Block {
            index: 0,
            offset: 0,
            len: 1
        }
    );
    check_plan_invariants(&p);
}

#[test]
fn plan_exactly_64_mib_and_neighbours() {
    let p = BlockPlan::new(64 * MIB).unwrap();
    assert_eq!((p.count(), p.block_size()), (1, 64 * MIB));
    assert_eq!(p.block(0).unwrap().len, 64 * MIB);

    let p = BlockPlan::new(64 * MIB - 1).unwrap();
    assert_eq!((p.count(), p.block_size()), (1, 64 * MIB));

    let p = BlockPlan::new(64 * MIB + 1).unwrap();
    assert_eq!((p.count(), p.block_size()), (2, 64 * MIB));
    assert_eq!(
        p.block(1).unwrap(),
        Block {
            index: 1,
            offset: 64 * MIB,
            len: 1
        }
    );
    assert_eq!(p.block_at(64 * MIB).unwrap().index, 1);
    assert_eq!(p.block_at(64 * MIB - 1).unwrap().index, 0);
}

#[test]
fn plan_200_gb() {
    let size = 200_000_000_000u64;
    let p = BlockPlan::new(size).unwrap();
    // ceil(200e9 / 50,000) = 4 MB < 64 MiB, so the floor wins.
    assert_eq!(p.block_size(), 64 * MIB);
    assert_eq!(p.count(), 2_981); // ceil(200e9 / 67,108,864)
    assert_eq!(p.block(2_980).unwrap().len, size - 2_980 * 64 * MIB);
    check_plan_invariants(&p);
}

#[test]
fn plan_where_the_50000_block_limit_takes_over() {
    // Largest size that still fits 64 MiB blocks.
    let edge = 64 * MIB * MAX_BLOCKS;
    let p = BlockPlan::new(edge).unwrap();
    assert_eq!(
        (p.block_size(), u64::from(p.count())),
        (64 * MIB, MAX_BLOCKS)
    );
    // One more byte needs bigger blocks: 64 MiB + 1 rounds up to 68 MiB.
    let p = BlockPlan::new(edge + 1).unwrap();
    assert_eq!(p.block_size(), 68 * MIB);
    check_plan_invariants(&p);
}

#[test]
fn plan_max_blob_size() {
    assert_eq!(MAX_BLOB_SIZE, 4_000 * MIB * 50_000);
    assert_eq!(MAX_BLOB_SIZE, 209_715_200_000_000); // ≈ 190.7 TiB
    let p = BlockPlan::new(MAX_BLOB_SIZE).unwrap();
    assert_eq!(p.block_size(), MAX_BLOCK_SIZE);
    assert_eq!(u64::from(p.count()), MAX_BLOCKS);
    assert_eq!(p.block(49_999).unwrap().len, MAX_BLOCK_SIZE);
    check_plan_invariants(&p);

    assert_eq!(
        BlockPlan::new(MAX_BLOB_SIZE + 1),
        Err(PlanError::TooLarge {
            size: MAX_BLOB_SIZE + 1
        })
    );
    assert!(BlockPlan::new(u64::MAX).is_err());
}

#[test]
fn plan_invariants_over_many_sizes() {
    let mut rng = Rng(7);
    let mut sizes = vec![
        2,
        4 * MIB,
        128 * MIB,
        1 << 40,
        64 * MIB * MAX_BLOCKS - 1,
        MAX_BLOB_SIZE - 1,
    ];
    for _ in 0..5_000 {
        // Log-uniform over 1 byte .. max blob size.
        let bits = rng.below(48) as u32;
        sizes.push((rng.next() >> (64 - bits.max(1))).min(MAX_BLOB_SIZE));
    }
    for size in sizes {
        let p = BlockPlan::new(size).unwrap();
        check_plan_invariants(&p);
    }
}

#[test]
fn plan_blocks_iterator_is_contiguous() {
    let p = BlockPlan::new(5 * 64 * MIB + 17).unwrap();
    let mut next = 0;
    for (i, b) in p.blocks().enumerate() {
        assert_eq!(b.index as usize, i);
        assert_eq!(b.offset, next);
        next = b.range().end;
    }
    assert_eq!(next, p.size());
}

#[test]
fn explicit_block_size_plans() {
    let p = BlockPlan::with_block_size(10, 3).unwrap();
    assert_eq!(p.count(), 4);
    assert_eq!(p.block(3).unwrap().len, 1);
    assert!(BlockPlan::with_block_size(10, 0).is_err());
    assert!(BlockPlan::with_block_size(10, MAX_BLOCK_SIZE + 1).is_err());
    assert!(BlockPlan::with_block_size(MAX_BLOCKS + 1, 1).is_err());
    assert!(BlockPlan::with_block_size(MAX_BLOCKS, 1).is_ok());
}

// --- block IDs -------------------------------------------------------------

#[test]
fn block_ids_are_equal_length_base64_and_round_trip() {
    let tag = 0x0123_4567_89ab_cdef;
    for index in [0u32, 1, 2, 255, 256, 49_999, 50_000, u32::MAX] {
        let id = block_id(tag, index);
        assert_eq!(id.len(), BLOCK_ID_LEN, "{id}");
        assert!(!id.contains('='));
        assert_eq!(parse_block_id(&id), Some((tag, index)));
        let q = block_id_query_value(&id);
        assert!(!q.contains('+') && !q.contains('/') && !q.contains('='));
    }
    const { assert!(BLOCK_ID_RAW_LEN <= 64) }; // Azure: pre-encoded ID ≤ 64 bytes.
    assert_ne!(block_id(1, 0), block_id(2, 0));
    assert_eq!(parse_block_id("AAAAAA=="), None);
    assert_eq!(parse_block_id("not base64 at all"), None);
}

#[test]
fn block_id_query_value_escapes() {
    // All-ones raw bytes encode to all '/'.
    let id = block_id(u64::MAX, u32::MAX);
    assert_eq!(id, "////////////////");
    assert_eq!(block_id_query_value(&id), "%2F".repeat(16));
    assert_eq!(block_id_query_value("a+b/c="), "a%2Bb%2Fc%3D");
}

// --- headers ---------------------------------------------------------------

#[test]
fn header_encodings_of_check_value() {
    // 0xae8b14860a799888: LE bytes 88 98 79 0a 86 14 8b ae; BE bytes ae 8b 14 86 0a 79 98 88.
    assert_eq!(azure_content_crc64(CHECK), "iJh5CoYUi64=");
    assert_eq!(s3_checksum_crc64nvme(CHECK), "rosUhgp5mIg=");
    assert_eq!(parse_azure_content_crc64("iJh5CoYUi64=").unwrap(), CHECK);
    assert_eq!(parse_s3_checksum_crc64nvme("rosUhgp5mIg=").unwrap(), CHECK);
    assert_eq!(azure_to_s3("iJh5CoYUi64=").unwrap(), "rosUhgp5mIg=");
}

#[test]
fn azure_put_block_sample_parses_unpadded() {
    // Microsoft's Put Block sample response: `x-ms-content-crc64: 77uWZTolTHU` (no padding).
    let v = parse_azure_content_crc64("77uWZTolTHU").unwrap();
    assert_eq!(
        v.to_le_bytes(),
        [0xef, 0xbb, 0x96, 0x65, 0x3a, 0x25, 0x4c, 0x75]
    );
    assert_eq!(azure_content_crc64(v), "77uWZTolTHU=");
    assert_eq!(parse_azure_content_crc64("77uWZTolTHU=").unwrap(), v);
}

#[test]
fn header_round_trips_and_errors() {
    let mut rng = Rng(8);
    for _ in 0..1_000 {
        let v = rng.next();
        assert_eq!(
            parse_azure_content_crc64(&azure_content_crc64(v)).unwrap(),
            v
        );
        assert_eq!(
            parse_s3_checksum_crc64nvme(&s3_checksum_crc64nvme(v)).unwrap(),
            v
        );
        assert_eq!(azure_content_crc64(v).len(), 12);
    }
    assert!(parse_azure_content_crc64("AAAA").is_err()); // 3 bytes
    assert!(parse_azure_content_crc64("AAAAAAAAAAAAAAAA").is_err()); // 12 bytes
    assert!(parse_s3_checksum_crc64nvme("!!!!").is_err());
}

// --- single pass -----------------------------------------------------------

fn expect_digest(data: &[u8], d: &FileDigest) {
    assert_eq!(d.size, data.len() as u64);
    assert_eq!(d.blake3, blake3::hash(data));
    assert_eq!(d.crc64, crc64_nvme(data));
    assert_eq!(d.crc64, reference_crc(data));
    assert_eq!(d.blocks.len(), d.plan.count() as usize);
    for (i, b) in d.blocks.iter().enumerate() {
        assert_eq!(b.block, d.plan.block(i as u32).unwrap());
        let r = b.block.range();
        assert_eq!(b.crc64, crc64_nvme(&data[r.start as usize..r.end as usize]));
        assert_eq!(
            parse_azure_content_crc64(&b.azure_header()).unwrap(),
            b.crc64
        );
    }
    assert_eq!(
        combine_all(d.blocks.iter().map(|b| (b.crc64, b.block.len))),
        d.crc64
    );
    assert_eq!(
        parse_s3_checksum_crc64nvme(&d.s3_checksum()).unwrap(),
        d.crc64
    );
}

#[test]
fn single_pass_small_blocks_many_shapes() {
    let mut rng = Rng(9);
    for &n in &[0usize, 1, 999, 1000, 1001, 4096, 65_537, 3_000_001] {
        let data = rng.bytes(n);
        for &block in &[1u64, 1000, 4096 + 7, 1 << 20, 64 * MIB] {
            let Ok(plan) = BlockPlan::with_block_size(n as u64, block) else {
                continue; // too many blocks for this pairing
            };
            for &chunk in &[1usize, 333, 1 << 16, 3 << 20] {
                if n / chunk > 20_000 {
                    continue; // keep the test quick
                }
                for parallel in [false, true] {
                    let opts = Options {
                        chunk_size: chunk,
                        queue_depth: 2,
                        parallel,
                    };
                    let d = hash_reader_with_plan(Cursor::new(&data), plan, opts).unwrap();
                    expect_digest(&data, &d);
                }
            }
        }
    }
}

#[test]
fn push_api_matches_reader_api() {
    let mut rng = Rng(10);
    let data = rng.bytes(2_500_000);
    let plan = BlockPlan::with_block_size(data.len() as u64, 700_000).unwrap();
    let mut h = IntegrityHasher::with_plan(plan, true);
    let mut pos = 0;
    while pos < data.len() {
        let n = (rng.below(300_000) as usize).min(data.len() - pos);
        h.update(&data[pos..pos + n]).unwrap();
        pos += n;
    }
    assert_eq!(h.completed_blocks().len(), 4);
    let d = h.finish().unwrap();
    expect_digest(&data, &d);
    let d2 = hash_reader_with_plan(Cursor::new(&data), plan, Options::default()).unwrap();
    assert_eq!(d, d2);
}

#[test]
fn default_plan_multi_block_file() {
    // Two full 64 MiB blocks and a ragged tail, through the real planner and a real file.
    let mut rng = Rng(11);
    let data = rng.bytes((128 * MIB + 12_345) as usize);
    let path = std::env::temp_dir().join(format!(
        "omnifile-integrity-test-{}-{}",
        std::process::id(),
        rng.next()
    ));
    {
        let mut f = std::fs::File::create(&path).unwrap();
        f.write_all(&data).unwrap();
    }
    let d = hash_file(&path, Options::default());
    std::fs::remove_file(&path).unwrap();
    let d = d.unwrap();
    assert_eq!(d.plan.block_size(), 64 * MIB);
    assert_eq!(d.blocks.len(), 3);
    assert_eq!(d.blocks[2].block.len, 12_345);
    expect_digest(&data, &d);
}

#[test]
fn reader_shorter_than_size_is_an_error() {
    let data = vec![7u8; 1000];
    let err = hash_reader(Cursor::new(&data), 1001, Options::default()).unwrap_err();
    assert!(matches!(
        err,
        IntegrityError::SizeMismatch {
            expected: 1001,
            actual: 1000
        }
    ));
}

#[test]
fn reader_longer_than_size_is_an_error() {
    let data = vec![7u8; 1000];
    let err = hash_reader(Cursor::new(&data), 999, Options::default()).unwrap_err();
    assert!(matches!(
        err,
        IntegrityError::SizeMismatch { expected: 999, .. }
    ));
}

#[test]
fn reader_io_error_propagates() {
    struct Failing(usize);
    impl Read for Failing {
        fn read(&mut self, buf: &mut [u8]) -> std::io::Result<usize> {
            if self.0 == 0 {
                return Err(std::io::Error::other("disk on fire"));
            }
            let n = buf.len().min(self.0);
            buf[..n].fill(1);
            self.0 -= n;
            Ok(n)
        }
    }
    let opts = Options {
        chunk_size: 100,
        queue_depth: 1,
        parallel: false,
    };
    let err = hash_reader(Failing(250), 1000, opts).unwrap_err();
    assert!(matches!(err, IntegrityError::Io(_)), "{err}");
}

#[test]
fn push_api_rejects_overrun_and_short_finish() {
    let mut h = IntegrityHasher::new(10).unwrap();
    h.update(b"12345").unwrap();
    assert!(h.update(b"123456").is_err());
    assert_eq!(h.position(), 5);
    assert!(matches!(
        h.clone().finish(),
        Err(IntegrityError::SizeMismatch {
            expected: 10,
            actual: 5
        })
    ));
    h.update(b"67890").unwrap();
    let d = h.finish().unwrap();
    assert_eq!(d.crc64, crc64_nvme(b"1234567890"));
}

#[test]
fn empty_file_digest() {
    let d = hash_reader(Cursor::new(Vec::new()), 0, Options::default()).unwrap();
    assert_eq!(d.crc64, 0);
    assert!(d.blocks.is_empty());
    assert_eq!(d.blake3, blake3::hash(b""));
    assert_eq!(d.s3_checksum(), "AAAAAAAAAAA=");
}
