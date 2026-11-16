//! Single-pass file digest: BLAKE3 + per-block CRC-64/NVME + whole-file
//! CRC-64/NVME, reading every byte once.
//!
//! [`hash_reader`] / [`hash_file`] run a reader thread that fills a small pool
//! of buffers while the calling thread hashes the previous ones, so I/O and
//! hashing overlap. Each chunk lies inside one block. Per chunk, BLAKE3
//! (`update_rayon`, multi-threaded) and the CRC (one thread) run side by side
//! with `rayon::join`; chunk CRCs are combined into block CRCs, and block CRCs
//! into the file CRC.
//!
//! Measured on an Apple M4 Max (16 threads), release build: crc-fast CRC-64/NVME
//! ≈ 69 GB/s on one thread, BLAKE3 ≈ 2.3 GB/s on one thread and ≈ 28 GB/s with
//! `update_rayon`. BLAKE3 is the bottleneck, so the CRC is not split further.
//!
//! [`IntegrityHasher`] is the same computation as a push API, for callers
//! that already hold the bytes (for example an uploader that reads a block,
//! hashes it and sends it).

use std::fmt;
use std::fs::File;
use std::io::{self, Read};
use std::path::Path;
use std::sync::mpsc;
use std::thread;

use crate::blocks::{Block, BlockPlan, PlanError};
use crate::crc64::{Crc64Combiner, crc64_nvme};
use crate::headers;

/// Inputs shorter than this are hashed on the calling thread only.
const PARALLEL_MIN: usize = 1 << 20;

#[derive(Debug)]
pub enum IntegrityError {
    Io(io::Error),
    Plan(PlanError),
    /// The data did not have the length the plan was made for: the file
    /// changed while it was read, or the caller passed the wrong size. When
    /// the data was longer, `actual` is a lower bound.
    SizeMismatch {
        expected: u64,
        actual: u64,
    },
}

impl fmt::Display for IntegrityError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            IntegrityError::Io(e) => write!(f, "read failed: {e}"),
            IntegrityError::Plan(e) => write!(f, "{e}"),
            IntegrityError::SizeMismatch { expected, actual } => write!(
                f,
                "expected {expected} bytes but read {actual}; the file changed while it was hashed"
            ),
        }
    }
}

impl std::error::Error for IntegrityError {
    fn source(&self) -> Option<&(dyn std::error::Error + 'static)> {
        match self {
            IntegrityError::Io(e) => Some(e),
            IntegrityError::Plan(e) => Some(e),
            IntegrityError::SizeMismatch { .. } => None,
        }
    }
}

impl From<io::Error> for IntegrityError {
    fn from(e: io::Error) -> Self {
        IntegrityError::Io(e)
    }
}

impl From<PlanError> for IntegrityError {
    fn from(e: PlanError) -> Self {
        IntegrityError::Plan(e)
    }
}

/// CRC of one planned block.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct BlockDigest {
    pub block: Block,
    pub crc64: u64,
}

impl BlockDigest {
    /// Value for the Put Block `x-ms-content-crc64` header.
    pub fn azure_header(&self) -> String {
        headers::azure_content_crc64(self.crc64)
    }
}

/// Everything the uploader and the catalog need about one file's bytes.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct FileDigest {
    pub size: u64,
    /// Whole-file BLAKE3 (dedupe key).
    pub blake3: blake3::Hash,
    /// Whole-file CRC-64/NVME (equal to S3's full-object `CRC64NVME`).
    pub crc64: u64,
    pub plan: BlockPlan,
    /// One entry per planned block, in order.
    pub blocks: Vec<BlockDigest>,
}

impl FileDigest {
    /// The whole-file CRC in S3's `x-amz-checksum-crc64nvme` form.
    pub fn s3_checksum(&self) -> String {
        headers::s3_checksum_crc64nvme(self.crc64)
    }
}

/// Tuning for the single-pass hasher.
#[derive(Clone, Copy, Debug)]
pub struct Options {
    /// Read size. Chunks never straddle a block boundary, so the last chunk of
    /// a block may be shorter.
    pub chunk_size: usize,
    /// Filled buffers that may wait for the hasher (read-ahead depth).
    pub queue_depth: usize,
    /// Use rayon for BLAKE3 and CRC within a chunk.
    pub parallel: bool,
}

impl Default for Options {
    fn default() -> Self {
        Self {
            chunk_size: 16 << 20,
            queue_depth: 4,
            parallel: true,
        }
    }
}

/// Push-style single-pass hasher over a file of known size.
#[derive(Clone, Debug)]
pub struct IntegrityHasher {
    plan: BlockPlan,
    parallel: bool,
    blake3: blake3::Hasher,
    block: Crc64Combiner,
    file: Crc64Combiner,
    blocks: Vec<BlockDigest>,
    pos: u64,
}

impl IntegrityHasher {
    pub fn new(size: u64) -> Result<Self, PlanError> {
        Self::with_parallel(size, true)
    }

    pub fn with_parallel(size: u64, parallel: bool) -> Result<Self, PlanError> {
        Ok(Self::with_plan(BlockPlan::new(size)?, parallel))
    }

    /// Hash against an explicit plan (for example one read back from an upload journal).
    pub fn with_plan(plan: BlockPlan, parallel: bool) -> Self {
        Self {
            plan,
            parallel,
            blake3: blake3::Hasher::new(),
            block: Crc64Combiner::new(),
            file: Crc64Combiner::new(),
            blocks: Vec::with_capacity(plan.count() as usize),
            pos: 0,
        }
    }

    pub fn plan(&self) -> &BlockPlan {
        &self.plan
    }

    /// Bytes consumed so far.
    pub fn position(&self) -> u64 {
        self.pos
    }

    /// Blocks completed so far (an uploader can send each as it completes).
    pub fn completed_blocks(&self) -> &[BlockDigest] {
        &self.blocks
    }

    /// Feed the next bytes. Fails, without consuming anything, if they run past the planned size.
    pub fn update(&mut self, mut data: &[u8]) -> Result<(), IntegrityError> {
        let size = self.plan.size();
        if self.pos + data.len() as u64 > size {
            return Err(IntegrityError::SizeMismatch {
                expected: size,
                actual: self.pos + data.len() as u64,
            });
        }
        while !data.is_empty() {
            let block = self.plan.block_at(self.pos).expect("pos < size");
            let room = (block.range().end - self.pos) as usize;
            let (head, rest) = data.split_at(room.min(data.len()));
            self.absorb(head, block);
            data = rest;
        }
        Ok(())
    }

    /// `data` lies within `block`.
    fn absorb(&mut self, data: &[u8], block: Block) {
        let crc = if self.parallel && data.len() >= PARALLEL_MIN {
            let hasher = &mut self.blake3;
            let ((), crc) = rayon::join(
                || {
                    hasher.update_rayon(data);
                },
                || crc64_nvme(data),
            );
            crc
        } else {
            self.blake3.update(data);
            crc64_nvme(data)
        };
        self.block.push(crc, data.len() as u64);
        self.pos += data.len() as u64;
        if self.pos == block.range().end {
            debug_assert_eq!(self.block.len(), block.len);
            let crc64 = self.block.crc();
            self.blocks.push(BlockDigest { block, crc64 });
            self.file.push(crc64, block.len);
            self.block = Crc64Combiner::new();
        }
    }

    pub fn finish(self) -> Result<FileDigest, IntegrityError> {
        if self.pos != self.plan.size() {
            return Err(IntegrityError::SizeMismatch {
                expected: self.plan.size(),
                actual: self.pos,
            });
        }
        debug_assert_eq!(self.blocks.len(), self.plan.count() as usize);
        Ok(FileDigest {
            size: self.plan.size(),
            blake3: self.blake3.finalize(),
            crc64: self.file.crc(),
            plan: self.plan,
            blocks: self.blocks,
        })
    }
}

/// Hash a file in one pass.
///
/// The size is taken from the open file's metadata; if the file grows or
/// shrinks while it is read, this returns [`IntegrityError::SizeMismatch`].
pub fn hash_file(path: impl AsRef<Path>, opts: Options) -> Result<FileDigest, IntegrityError> {
    let file = File::open(path)?;
    let size = file.metadata()?.len();
    hash_reader(file, size, opts)
}

/// Hash exactly `size` bytes from `reader` in one pass, with read-ahead on a
/// separate thread. Fails if the reader yields fewer or more bytes.
pub fn hash_reader<R: Read + Send>(
    reader: R,
    size: u64,
    opts: Options,
) -> Result<FileDigest, IntegrityError> {
    hash_reader_with_plan(reader, BlockPlan::new(size)?, opts)
}

/// [`hash_reader`] against an explicit plan; reads exactly `plan.size()` bytes.
pub fn hash_reader_with_plan<R: Read + Send>(
    reader: R,
    plan: BlockPlan,
    opts: Options,
) -> Result<FileDigest, IntegrityError> {
    let mut hasher = IntegrityHasher::with_plan(plan, opts.parallel);
    let chunk = opts.chunk_size.max(1);
    let depth = opts.queue_depth.max(1);

    thread::scope(|s| {
        let (full_tx, full_rx) = mpsc::sync_channel::<Result<Vec<u8>, IntegrityError>>(depth);
        let (free_tx, free_rx) = mpsc::channel::<Vec<u8>>();
        // Buffers in flight: `depth` queued + one being read + one being hashed.
        let pool = depth + 2;

        s.spawn(move || read_loop(reader, plan, chunk, pool, full_tx, free_rx));

        for msg in full_rx {
            let buf = msg?;
            hasher.update(&buf)?;
            // The reader may have finished; a closed pool is fine.
            let _ = free_tx.send(buf);
        }
        Ok::<(), IntegrityError>(())
    })?;

    hasher.finish()
}

fn read_loop<R: Read>(
    mut reader: R,
    plan: BlockPlan,
    chunk: usize,
    pool: usize,
    full_tx: mpsc::SyncSender<Result<Vec<u8>, IntegrityError>>,
    free_rx: mpsc::Receiver<Vec<u8>>,
) {
    let size = plan.size();
    let mut pos = 0u64;
    let mut allocated = 0usize;
    while pos < size {
        let block = plan.block_at(pos).expect("pos < size");
        let want = (chunk as u64).min(block.range().end - pos) as usize;
        let mut buf = match free_rx.try_recv() {
            Ok(b) => b,
            Err(_) if allocated < pool => {
                allocated += 1;
                Vec::with_capacity(chunk)
            }
            Err(_) => match free_rx.recv() {
                Ok(b) => b,
                Err(_) => return, // hasher gone
            },
        };
        buf.resize(want, 0);
        let n = match read_full(&mut reader, &mut buf) {
            Ok(n) => n,
            Err(e) => {
                let _ = full_tx.send(Err(e.into()));
                return;
            }
        };
        if n < want {
            let _ = full_tx.send(Err(IntegrityError::SizeMismatch {
                expected: size,
                actual: pos + n as u64,
            }));
            return;
        }
        pos += n as u64;
        if full_tx.send(Ok(buf)).is_err() {
            return;
        }
    }
    // The file must end exactly here.
    let mut probe = [0u8; 1];
    match read_full(&mut reader, &mut probe) {
        Ok(0) => {}
        Ok(_) => {
            let _ = full_tx.send(Err(IntegrityError::SizeMismatch {
                expected: size,
                actual: size + 1,
            }));
        }
        Err(e) => {
            let _ = full_tx.send(Err(e.into()));
        }
    }
}

/// Read until `buf` is full or EOF; returns bytes read.
fn read_full<R: Read>(reader: &mut R, buf: &mut [u8]) -> io::Result<usize> {
    let mut filled = 0;
    while filled < buf.len() {
        match reader.read(&mut buf[filled..]) {
            Ok(0) => break,
            Ok(n) => filled += n,
            Err(e) if e.kind() == io::ErrorKind::Interrupted => {}
            Err(e) => return Err(e),
        }
    }
    Ok(filled)
}
