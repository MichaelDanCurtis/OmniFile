//! Azure block-blob upload planning and block IDs.
//!
//! Limits, from Microsoft Learn "Put Block" (Remarks) and "Understanding block
//! blobs, append blobs, and page blobs", for service version 2019-12-12 and
//! later (checked 2026-10-05):
//!
//! * maximum block size via Put Block: **4,000 MiB**;
//! * a block blob holds at most **50,000 committed blocks**;
//! * maximum blob size via Put Block List: **4,000 MiB × 50,000 blocks**
//!   (≈ 190.7 TiB);
//! * at most 100,000 uncommitted blocks per blob;
//! * a block ID is a Base64 string whose pre-encoded value is ≤ 64 bytes, and
//!   every block ID of one blob must have the same length. In the URI it must
//!   also be URL-encoded.
//!
//! Sources:
//! <https://learn.microsoft.com/rest/api/storageservices/put-block>,
//! <https://learn.microsoft.com/rest/api/storageservices/understanding-block-blobs--append-blobs--and-page-blobs>.

use std::fmt;
use std::ops::Range;

use base64::Engine as _;
use base64::engine::general_purpose::STANDARD;

pub const MIB: u64 = 1 << 20;

/// Smallest block we plan (build plan M1): fewer, larger requests.
pub const MIN_BLOCK_SIZE: u64 = 64 * MIB;

/// Block sizes are rounded up to a multiple of this.
pub const BLOCK_ALIGN: u64 = 4 * MIB;

/// Azure: maximum committed blocks per block blob.
pub const MAX_BLOCKS: u64 = 50_000;

/// Azure (2019-12-12 and later): maximum Put Block size.
pub const MAX_BLOCK_SIZE: u64 = 4_000 * MIB;

/// Azure (2019-12-12 and later): maximum block blob size via Put Block List.
pub const MAX_BLOB_SIZE: u64 = MAX_BLOCK_SIZE * MAX_BLOCKS;

// The planner relies on these; a change to any constant must keep them true.
const _: () = assert!(MIN_BLOCK_SIZE.is_multiple_of(BLOCK_ALIGN));
const _: () = assert!(MAX_BLOCK_SIZE.is_multiple_of(BLOCK_ALIGN));

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum PlanError {
    /// The file is larger than one Azure block blob can hold.
    TooLarge { size: u64 },
    /// An explicit block size of 0 or above Azure's maximum.
    BadBlockSize { block_size: u64 },
    /// An explicit block size that would need more than 50,000 blocks.
    TooManyBlocks { size: u64, block_size: u64 },
}

impl fmt::Display for PlanError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            PlanError::TooLarge { size } => write!(
                f,
                "{size} bytes exceeds the Azure block blob limit of {MAX_BLOB_SIZE} bytes \
                 ({MAX_BLOCKS} blocks of {MAX_BLOCK_SIZE} bytes)"
            ),
            PlanError::BadBlockSize { block_size } => write!(
                f,
                "block size {block_size} is outside Azure's 1..={MAX_BLOCK_SIZE} byte range"
            ),
            PlanError::TooManyBlocks { size, block_size } => write!(
                f,
                "{size} bytes in {block_size}-byte blocks needs more than {MAX_BLOCKS} blocks"
            ),
        }
    }
}

impl std::error::Error for PlanError {}

/// One block of a plan.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct Block {
    pub index: u32,
    pub offset: u64,
    pub len: u64,
}

impl Block {
    pub fn range(&self) -> Range<u64> {
        self.offset..self.offset + self.len
    }
}

/// How a file of `size` bytes is cut into Azure blocks.
///
/// `block_size = max(64 MiB, ceil(size / 50,000))`, rounded up to a multiple of
/// 4 MiB. Every block has `block_size` bytes except the last, which holds the
/// remainder. A 0-byte file has no blocks (commit an empty block list, or use
/// Put Blob).
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct BlockPlan {
    size: u64,
    block_size: u64,
    count: u32,
}

impl BlockPlan {
    pub fn new(size: u64) -> Result<Self, PlanError> {
        if size > MAX_BLOB_SIZE {
            return Err(PlanError::TooLarge { size });
        }
        let needed = size.div_ceil(MAX_BLOCKS);
        let block_size = needed.max(MIN_BLOCK_SIZE).next_multiple_of(BLOCK_ALIGN);
        // size <= MAX_BLOB_SIZE ⇒ needed <= MAX_BLOCK_SIZE, which is 4 MiB aligned.
        debug_assert!(block_size <= MAX_BLOCK_SIZE);
        let count = size.div_ceil(block_size);
        debug_assert!(count <= MAX_BLOCKS);
        Ok(Self {
            size,
            block_size,
            count: count as u32,
        })
    }

    /// A plan with an explicit block size, for example the one recorded in an
    /// upload journal, so a resumed upload keeps its original block layout
    /// even if the planning policy has changed since. Checked against Azure's
    /// limits but not against the 64 MiB / 4 MiB policy.
    pub fn with_block_size(size: u64, block_size: u64) -> Result<Self, PlanError> {
        if block_size == 0 || block_size > MAX_BLOCK_SIZE {
            return Err(PlanError::BadBlockSize { block_size });
        }
        let count = size.div_ceil(block_size);
        if count > MAX_BLOCKS {
            return Err(PlanError::TooManyBlocks { size, block_size });
        }
        Ok(Self {
            size,
            block_size,
            count: count as u32,
        })
    }

    pub fn size(&self) -> u64 {
        self.size
    }

    /// Size of every block except possibly the last.
    pub fn block_size(&self) -> u64 {
        self.block_size
    }

    pub fn count(&self) -> u32 {
        self.count
    }

    pub fn is_empty(&self) -> bool {
        self.count == 0
    }

    /// Block `index`, or `None` past the end.
    pub fn block(&self, index: u32) -> Option<Block> {
        if index >= self.count {
            return None;
        }
        let offset = u64::from(index) * self.block_size;
        let len = self.block_size.min(self.size - offset);
        Some(Block { index, offset, len })
    }

    /// The block that contains byte `offset`.
    pub fn block_at(&self, offset: u64) -> Option<Block> {
        if offset >= self.size {
            return None;
        }
        self.block((offset / self.block_size) as u32)
    }

    pub fn blocks(&self) -> impl ExactSizeIterator<Item = Block> + '_ {
        (0..self.count).map(|i| self.block(i).expect("index < count"))
    }
}

// ---------------------------------------------------------------------------
// Block IDs
// ---------------------------------------------------------------------------

/// Raw (pre-Base64) block ID length: 8-byte upload tag + 4-byte index.
pub const BLOCK_ID_RAW_LEN: usize = 12;

/// Encoded block ID length. 12 raw bytes encode to 16 Base64 characters with
/// no `=` padding.
pub const BLOCK_ID_LEN: usize = 16;

/// Azure block ID for block `index` of the upload tagged `upload_tag`.
///
/// The raw ID is `upload_tag` (big-endian u64) followed by `index`
/// (big-endian u32): 12 bytes, well under Azure's 64-byte limit, and the same
/// length for every block. Standard Base64 of 12 bytes is always 16 characters.
///
/// The tag should come from the upload journal and change whenever the
/// upload restarts on different content. A resumed upload of the same
/// content reuses its tag, so re-sent blocks replace their uncommitted copies;
/// a stale block from an abandoned attempt can never match an ID in a newer
/// block list.
///
/// Standard Base64 can contain `+` and `/`; use [`block_id_query_value`] when
/// building the `blockid=` query parameter.
pub fn block_id(upload_tag: u64, index: u32) -> String {
    let mut raw = [0u8; BLOCK_ID_RAW_LEN];
    raw[..8].copy_from_slice(&upload_tag.to_be_bytes());
    raw[8..].copy_from_slice(&index.to_be_bytes());
    STANDARD.encode(raw)
}

/// Inverse of [`block_id`]: `(upload_tag, index)`, or `None` if `id` is not one of ours.
pub fn parse_block_id(id: &str) -> Option<(u64, u32)> {
    if id.len() != BLOCK_ID_LEN {
        return None;
    }
    let raw = STANDARD.decode(id).ok()?;
    let raw: [u8; BLOCK_ID_RAW_LEN] = raw.try_into().ok()?;
    let tag = u64::from_be_bytes(raw[..8].try_into().ok()?);
    let index = u32::from_be_bytes(raw[8..].try_into().ok()?);
    Some((tag, index))
}

/// Percent-encode a Base64 block ID for the `blockid=` query parameter
/// (Put Block: "The Base64 string must be URL-encoded").
pub fn block_id_query_value(id: &str) -> String {
    let mut out = String::with_capacity(id.len() + 8);
    for c in id.chars() {
        match c {
            '+' => out.push_str("%2B"),
            '/' => out.push_str("%2F"),
            '=' => out.push_str("%3D"),
            c => out.push(c),
        }
    }
    out
}
