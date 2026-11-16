//! OmniFile Gateway edge hashing and integrity.
//!
//! * [`crc64`]: CRC-64/NVME (Azure `x-ms-content-crc64`, S3 `CRC64NVME`) and
//!   the combine operation that turns per-block CRCs into the whole-object
//!   CRC without re-reading data.
//! * [`blocks`]: Azure block planning and block IDs.
//! * [`headers`]: Azure (little-endian) and S3 (big-endian) Base64 encodings.
//! * [`digest`]: single-pass BLAKE3 + per-block CRC + whole-file CRC.
//!
//! See `AGENTS.md` "Integrity: CRC64-NVME end to end" and decision 24 in
//! `docs/DECISIONS.md`.
//!
//! Block planning is Azure-only: the edge never uploads to S3 in v1, because
//! v1 migrates S3 -> Azure only (decision 23) and new writes go to Azure. An S3
//! multipart planner (10,000 parts, 5 MiB-5 GiB) is needed only if Azure -> S3
//! arrives in v2.

pub mod blocks;
pub mod crc64;
pub mod digest;
pub mod headers;

pub use blocks::{Block, BlockPlan, PlanError, block_id, block_id_query_value, parse_block_id};
pub use crc64::{Crc64, Crc64Combiner, combine, combine_all, crc64_nvme};
pub use digest::{
    BlockDigest, FileDigest, IntegrityError, IntegrityHasher, Options, hash_file, hash_reader,
    hash_reader_with_plan,
};
pub use headers::{
    azure_content_crc64, parse_azure_content_crc64, parse_s3_checksum_crc64nvme,
    s3_checksum_crc64nvme,
};
