//! Resumable upload journal: which blocks of an upload are durable in Azure.
//!
//! One file per upload, JSON lines, append-only, `fsync`ed after every
//! record:
//!
//! ```text
//! {"v":1,"target":"https://acct/c/key","size":…,"block_size":…,"tag":…,"fingerprint":"…"}
//! {"block":0,"crc":"9a…"}            one per block Azure acknowledged (201)
//! {"committed":true,"etag":"…","version_id":"…"}   after Put Block List
//! ```
//!
//! * A block is recorded only after Azure answered 201 to its Put Block, so
//!   a recorded block is staged (uncommitted) in Azure. Staged blocks live
//!   for a week; if they were dropped, Put Block List fails with
//!   `InvalidBlockList` and the uploader starts over with a new tag.
//! * The header's `fingerprint` (the caller's identity for the bytes, for
//!   example the BLAKE3 or the content version) must match on resume;
//!   otherwise the journal is restarted with a fresh `tag`. Block IDs embed the
//!   tag, so a block from an abandoned attempt can never be named by a newer
//!   block list.
//! * A torn last line (crash mid-append) is truncated away on open.

use std::collections::BTreeMap;
use std::fs::{File, OpenOptions};
use std::io::{BufRead, BufReader, Seek, SeekFrom, Write};
use std::path::{Path, PathBuf};

use serde::{Deserialize, Serialize};

use crate::error::{BlobError, Result};

const VERSION: u32 = 1;

/// What an upload is: if any of this differs on resume, the journal restarts.
#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub struct JournalHeader {
    pub v: u32,
    /// The blob URL without its query (no SAS: credentials are renewed).
    pub target: String,
    pub size: u64,
    pub block_size: u64,
    /// Upload tag embedded in every block ID.
    pub tag: u64,
    /// Caller's identity for the bytes (BLAKE3, content version, …).
    pub fingerprint: String,
}

#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub struct CommitRecord {
    pub committed: bool,
    pub etag: Option<String>,
    pub version_id: Option<String>,
}

#[derive(Debug, Serialize, Deserialize)]
struct BlockRecord {
    block: u32,
    crc: String,
}

#[derive(Debug, Deserialize)]
#[serde(untagged)]
enum Line {
    Block(BlockRecord),
    Commit(CommitRecord),
}

pub struct UploadJournal {
    path: PathBuf,
    file: File,
    header: JournalHeader,
    blocks: BTreeMap<u32, u64>,
    commit: Option<CommitRecord>,
}

impl UploadJournal {
    /// Open the journal at `path` for an upload of `target`/`size`/`block_size`
    /// with `fingerprint`. Resumes when the existing header matches; otherwise
    /// starts a new journal with a fresh tag.
    pub fn open(
        path: &Path,
        target: &str,
        size: u64,
        block_size: u64,
        fingerprint: &str,
    ) -> Result<Self> {
        if let Some(j) = Self::try_resume(path, target, size, block_size, fingerprint)? {
            return Ok(j);
        }
        let header = JournalHeader {
            v: VERSION,
            target: target.to_owned(),
            size,
            block_size,
            tag: new_tag(),
            fingerprint: fingerprint.to_owned(),
        };
        Self::create(path, header)
    }

    fn try_resume(
        path: &Path,
        target: &str,
        size: u64,
        block_size: u64,
        fingerprint: &str,
    ) -> Result<Option<Self>> {
        let file = match OpenOptions::new().read(true).append(true).open(path) {
            Ok(f) => f,
            Err(e) if e.kind() == std::io::ErrorKind::NotFound => return Ok(None),
            Err(e) => return Err(e.into()),
        };
        let mut reader = BufReader::new(file.try_clone()?);
        let mut line = String::new();
        let mut good_len: u64 = 0;
        if reader.read_line(&mut line)? == 0 || !line.ends_with('\n') {
            return Ok(None);
        }
        let Ok(header) = serde_json::from_str::<JournalHeader>(&line) else {
            return Ok(None);
        };
        if header.v != VERSION
            || header.target != target
            || header.size != size
            || header.block_size != block_size
            || header.fingerprint != fingerprint
        {
            tracing::info!(path = %path.display(), "upload journal does not match this upload; restarting it");
            return Ok(None);
        }
        good_len += line.len() as u64;
        let count = size.div_ceil(block_size);
        let mut blocks = BTreeMap::new();
        let mut commit = None;
        loop {
            line.clear();
            let n = reader.read_line(&mut line)?;
            if n == 0 || !line.ends_with('\n') {
                break;
            }
            match serde_json::from_str::<Line>(&line) {
                Ok(Line::Block(b)) if u64::from(b.block) < count => {
                    let Ok(crc) = u64::from_str_radix(&b.crc, 16) else {
                        break;
                    };
                    blocks.insert(b.block, crc);
                }
                Ok(Line::Commit(c)) => commit = Some(c),
                _ => break,
            }
            good_len += n as u64;
        }
        let file_len = file.metadata()?.len();
        if file_len != good_len {
            tracing::warn!(path = %path.display(), file_len, good_len, "truncating a torn upload journal");
            file.set_len(good_len)?;
            file.sync_data()?;
        }
        Ok(Some(Self {
            path: path.to_owned(),
            file,
            header,
            blocks,
            commit,
        }))
    }

    fn create(path: &Path, header: JournalHeader) -> Result<Self> {
        if let Some(dir) = path.parent() {
            std::fs::create_dir_all(dir)?;
        }
        let tmp = path.with_extension("journal-new");
        {
            let mut f = File::create(&tmp)?;
            let mut line = serde_json::to_string(&header).map_err(|e| journal_err(path, e))?;
            line.push('\n');
            f.write_all(line.as_bytes())?;
            f.sync_all()?;
        }
        std::fs::rename(&tmp, path)?;
        sync_dir(path);
        let mut file = OpenOptions::new().read(true).append(true).open(path)?;
        file.seek(SeekFrom::End(0))?;
        Ok(Self {
            path: path.to_owned(),
            file,
            header,
            blocks: BTreeMap::new(),
            commit: None,
        })
    }

    pub fn header(&self) -> &JournalHeader {
        &self.header
    }

    pub fn tag(&self) -> u64 {
        self.header.tag
    }

    pub fn path(&self) -> &Path {
        &self.path
    }

    /// Blocks already durable in Azure, with their CRC64-NVME.
    pub fn done_blocks(&self) -> &BTreeMap<u32, u64> {
        &self.blocks
    }

    pub fn commit(&self) -> Option<&CommitRecord> {
        self.commit.as_ref().filter(|c| c.committed)
    }

    /// Record that block `index` (with `crc`) is staged in Azure. Durable on return.
    pub fn mark_block(&mut self, index: u32, crc: u64) -> Result<()> {
        let rec = BlockRecord {
            block: index,
            crc: format!("{crc:016x}"),
        };
        self.append(&rec)?;
        self.blocks.insert(index, crc);
        Ok(())
    }

    /// Record that Put Block List succeeded. Durable on return.
    pub fn mark_committed(
        &mut self,
        etag: Option<String>,
        version_id: Option<String>,
    ) -> Result<()> {
        let rec = CommitRecord {
            committed: true,
            etag,
            version_id,
        };
        self.append(&rec)?;
        self.commit = Some(rec);
        Ok(())
    }

    /// Forget every staged block and start over with a new tag (Azure dropped
    /// the uncommitted blocks).
    pub fn reset(&mut self) -> Result<()> {
        let mut header = self.header.clone();
        header.tag = new_tag();
        *self = Self::create(&self.path.clone(), header)?;
        Ok(())
    }

    /// Delete a finished journal. Call after the service accepted `complete`.
    pub fn remove(path: &Path) -> Result<()> {
        match std::fs::remove_file(path) {
            Ok(()) => Ok(()),
            Err(e) if e.kind() == std::io::ErrorKind::NotFound => Ok(()),
            Err(e) => Err(e.into()),
        }
    }

    fn append<T: Serialize>(&mut self, rec: &T) -> Result<()> {
        let mut line = serde_json::to_string(rec).map_err(|e| journal_err(&self.path, e))?;
        line.push('\n');
        self.file.write_all(line.as_bytes())?;
        self.file.sync_data()?;
        Ok(())
    }
}

fn journal_err(path: &Path, e: impl std::fmt::Display) -> BlobError {
    BlobError::Journal {
        path: path.display().to_string(),
        detail: e.to_string(),
    }
}

fn new_tag() -> u64 {
    // Never 0, so a zeroed record can't look like a real tag.
    fastrand::u64(1..)
}

fn sync_dir(path: &Path) {
    if let Some(dir) = path.parent()
        && let Ok(d) = File::open(dir)
    {
        let _ = d.sync_all();
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn resume_keeps_blocks_and_tag() {
        let dir = tempfile::tempdir().unwrap();
        let p = dir.path().join("u.journal");
        let mut j = UploadJournal::open(&p, "http://x/a/b", 100, 10, "fp").unwrap();
        let tag = j.tag();
        j.mark_block(0, 0xabc).unwrap();
        j.mark_block(3, 0xdef).unwrap();
        drop(j);
        let j = UploadJournal::open(&p, "http://x/a/b", 100, 10, "fp").unwrap();
        assert_eq!(j.tag(), tag);
        assert_eq!(j.done_blocks().len(), 2);
        assert_eq!(j.done_blocks()[&3], 0xdef);
        assert!(j.commit().is_none());
    }

    #[test]
    fn different_fingerprint_restarts() {
        let dir = tempfile::tempdir().unwrap();
        let p = dir.path().join("u.journal");
        let mut j = UploadJournal::open(&p, "http://x/a/b", 100, 10, "fp1").unwrap();
        let tag = j.tag();
        j.mark_block(0, 1).unwrap();
        drop(j);
        let j = UploadJournal::open(&p, "http://x/a/b", 100, 10, "fp2").unwrap();
        assert_ne!(j.tag(), tag);
        assert!(j.done_blocks().is_empty());
    }

    #[test]
    fn torn_line_is_truncated() {
        let dir = tempfile::tempdir().unwrap();
        let p = dir.path().join("u.journal");
        let mut j = UploadJournal::open(&p, "t", 100, 10, "fp").unwrap();
        j.mark_block(1, 7).unwrap();
        drop(j);
        let mut f = OpenOptions::new().append(true).open(&p).unwrap();
        f.write_all(b"{\"block\":2,\"cr").unwrap();
        drop(f);
        let mut j = UploadJournal::open(&p, "t", 100, 10, "fp").unwrap();
        assert_eq!(j.done_blocks().keys().copied().collect::<Vec<_>>(), vec![1]);
        j.mark_block(2, 9).unwrap();
        j.mark_committed(Some("e".into()), None).unwrap();
        drop(j);
        let j = UploadJournal::open(&p, "t", 100, 10, "fp").unwrap();
        assert_eq!(j.done_blocks().len(), 2);
        assert_eq!(j.commit().unwrap().etag.as_deref(), Some("e"));
    }

    #[test]
    fn reset_changes_tag_and_clears() {
        let dir = tempfile::tempdir().unwrap();
        let p = dir.path().join("u.journal");
        let mut j = UploadJournal::open(&p, "t", 100, 10, "fp").unwrap();
        let tag = j.tag();
        j.mark_block(0, 1).unwrap();
        j.reset().unwrap();
        assert_ne!(j.tag(), tag);
        assert!(j.done_blocks().is_empty());
        drop(j);
        assert!(
            UploadJournal::open(&p, "t", 100, 10, "fp")
                .unwrap()
                .done_blocks()
                .is_empty()
        );
    }
}
