//! What the cache holds, in memory: per-chunk LRU, the header prefixes,
//! and which files they belong to. The placeholders on disk are the truth
//! (marker and map); this index decides what to evict next, and is rebuilt
//! from disk at start (`Cache::adopt_from_disk`).
//!
//! Headers are first in and last out: every chunk is evicted before any
//! header prefix.

use std::collections::{BTreeMap, HashMap};
use std::path::PathBuf;

/// What eviction needs to know about a file.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct FileMeta {
    /// Path relative to the data root.
    pub rel: PathBuf,
    /// The content version whose bytes are cached.
    pub version_id: u64,
    pub size: u64,
    /// The map's chunk size (log2).
    pub shift: u8,
    /// The resident prefix the catalog counts (a stub's, kept under a lease
    /// by the stubber): the cache never narrows below it. 0 for a remote
    /// object the edge never held.
    pub floor: u64,
}

#[derive(Default)]
struct FileEntry {
    meta: Option<FileMeta>,
    /// chunk → (bytes, tick)
    chunks: HashMap<u64, (u64, u64)>,
    /// (bytes, tick)
    header: Option<(u64, u64)>,
}

/// One eviction decision.
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum Victim {
    /// Clear and punch these chunks.
    Chunks {
        file_id: u64,
        meta: FileMeta,
        chunks: Vec<u64>,
        bytes: u64,
    },
    /// Evict everything of the file, its header included.
    File {
        file_id: u64,
        meta: FileMeta,
        bytes: u64,
    },
}

#[derive(Default)]
pub struct Index {
    files: HashMap<u64, FileEntry>,
    chunk_lru: BTreeMap<u64, (u64, u64)>,
    header_lru: BTreeMap<u64, u64>,
    tick: u64,
    used: u64,
    header_used: u64,
}

impl Index {
    pub fn new() -> Self {
        Self::default()
    }

    fn next_tick(&mut self) -> u64 {
        self.tick += 1;
        self.tick
    }

    /// Bytes the cache holds (chunks and headers).
    pub fn used(&self) -> u64 {
        self.used
    }

    /// Of [`Index::used`], the header prefixes.
    pub fn header_used(&self) -> u64 {
        self.header_used
    }

    pub fn files(&self) -> usize {
        self.files.len()
    }

    pub fn meta(&self, file_id: u64) -> Option<&FileMeta> {
        self.files.get(&file_id)?.meta.as_ref()
    }

    /// Record (or update) what a file is. A different content version or
    /// chunk size drops what was indexed before (chunk indexes would mean
    /// other byte ranges).
    pub fn set_meta(&mut self, file_id: u64, meta: FileMeta) {
        if self
            .files
            .get(&file_id)
            .and_then(|e| e.meta.as_ref())
            .is_some_and(|m| m.version_id != meta.version_id || m.shift != meta.shift)
        {
            self.forget(file_id);
        }
        self.files.entry(file_id).or_default().meta = Some(meta);
    }

    pub fn has_chunk(&self, file_id: u64, chunk: u64) -> bool {
        self.files
            .get(&file_id)
            .is_some_and(|e| e.chunks.contains_key(&chunk))
    }

    /// A chunk is now local (or was used again): most recently used.
    pub fn touch_chunk(&mut self, file_id: u64, chunk: u64, bytes: u64) {
        let t = self.next_tick();
        let e = self.files.entry(file_id).or_default();
        match e.chunks.insert(chunk, (bytes, t)) {
            Some((old_bytes, old_t)) => {
                self.chunk_lru.remove(&old_t);
                self.used = self.used - old_bytes + bytes;
            }
            None => self.used += bytes,
        }
        self.chunk_lru.insert(t, (file_id, chunk));
    }

    /// The file's resident prefix is `bytes` long (0: none).
    pub fn set_header(&mut self, file_id: u64, bytes: u64) {
        let t = self.next_tick();
        let e = self.files.entry(file_id).or_default();
        if let Some((old, old_t)) = e.header.take() {
            self.header_lru.remove(&old_t);
            self.used -= old;
            self.header_used -= old;
        }
        if bytes > 0 {
            e.header = Some((bytes, t));
            self.header_lru.insert(t, file_id);
            self.used += bytes;
            self.header_used += bytes;
        }
    }

    pub fn header(&self, file_id: u64) -> u64 {
        self.files
            .get(&file_id)
            .and_then(|e| e.header)
            .map_or(0, |(b, _)| b)
    }

    /// Chunks were punched.
    pub fn remove_chunks(&mut self, file_id: u64, chunks: &[u64]) {
        let Some(e) = self.files.get_mut(&file_id) else {
            return;
        };
        for c in chunks {
            if let Some((b, t)) = e.chunks.remove(c) {
                self.chunk_lru.remove(&t);
                self.used -= b;
            }
        }
    }

    /// Drop everything about a file (evicted, became resident in the
    /// catalog, deleted, or replaced by another version).
    pub fn forget(&mut self, file_id: u64) {
        let Some(e) = self.files.remove(&file_id) else {
            return;
        };
        for (_, (b, t)) in e.chunks {
            self.chunk_lru.remove(&t);
            self.used -= b;
        }
        if let Some((b, t)) = e.header {
            self.header_lru.remove(&t);
            self.used -= b;
            self.header_used -= b;
        }
    }

    /// Move a victim's entries to most recently used (an eviction that
    /// couldn't proceed now), so the next pass tries others first.
    pub fn retry_later(&mut self, v: &Victim) {
        match v {
            Victim::Chunks {
                file_id, chunks, ..
            } => {
                for c in chunks {
                    if let Some(&(b, _)) = self.files.get(file_id).and_then(|e| e.chunks.get(c)) {
                        self.touch_chunk(*file_id, *c, b);
                    }
                }
            }
            Victim::File { file_id, .. } => {
                let h = self.header(*file_id);
                if h > 0 {
                    self.set_header(*file_id, h);
                }
            }
        }
    }

    /// The least recently used entries worth at least `need` bytes: chunks
    /// first, grouped per file; header prefixes only once no chunk is left.
    /// Files without metadata can't be evicted and are skipped.
    pub fn victims(&self, need: u64) -> Vec<Victim> {
        let mut out: Vec<Victim> = Vec::new();
        let mut by_file: HashMap<u64, usize> = HashMap::new();
        let mut got = 0u64;
        for (file_id, chunk) in self.chunk_lru.values() {
            if got >= need {
                return out;
            }
            let Some(e) = self.files.get(file_id) else {
                continue;
            };
            let Some(meta) = &e.meta else { continue };
            let b = e.chunks[chunk].0;
            match by_file.get(file_id) {
                Some(&i) => {
                    if let Victim::Chunks { chunks, bytes, .. } = &mut out[i] {
                        chunks.push(*chunk);
                        *bytes += b;
                    }
                }
                None => {
                    by_file.insert(*file_id, out.len());
                    out.push(Victim::Chunks {
                        file_id: *file_id,
                        meta: meta.clone(),
                        chunks: vec![*chunk],
                        bytes: b,
                    });
                }
            }
            got += b;
        }
        for file_id in self.header_lru.values() {
            if got >= need {
                break;
            }
            let Some(e) = self.files.get(file_id) else {
                continue;
            };
            let Some(meta) = &e.meta else { continue };
            // Evicting the file frees its header and whatever chunks the
            // pass above didn't already count.
            let counted: u64 = match by_file.get(file_id) {
                Some(&i) => match &out[i] {
                    Victim::Chunks { bytes, .. } => *bytes,
                    _ => 0,
                },
                None => 0,
            };
            let all: u64 =
                e.chunks.values().map(|(b, _)| b).sum::<u64>() + e.header.map_or(0, |h| h.0);
            got += all - counted;
            out.push(Victim::File {
                file_id: *file_id,
                meta: meta.clone(),
                bytes: all,
            });
        }
        out
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn meta(v: u64) -> FileMeta {
        FileMeta {
            rel: PathBuf::from("s/f"),
            version_id: v,
            size: 100 << 20,
            shift: 22,
            floor: 0,
        }
    }

    #[test]
    fn lru_order_and_accounting() {
        let mut ix = Index::new();
        ix.set_meta(1, meta(1));
        ix.set_meta(2, meta(1));
        ix.set_header(1, 4096);
        ix.touch_chunk(1, 3, 4 << 20);
        ix.touch_chunk(2, 0, 4 << 20);
        ix.touch_chunk(1, 4, 4 << 20);
        assert_eq!(ix.used(), 4096 + (12 << 20));
        assert_eq!(ix.header_used(), 4096);
        // Re-use chunk 3: now chunk (2,0) is the oldest.
        ix.touch_chunk(1, 3, 4 << 20);
        let v = ix.victims(1);
        assert_eq!(
            v,
            vec![Victim::Chunks {
                file_id: 2,
                meta: meta(1),
                chunks: vec![0],
                bytes: 4 << 20
            }]
        );
        // Chunks of one file are grouped.
        let v = ix.victims(9 << 20);
        assert_eq!(v.len(), 2);
        assert!(
            matches!(&v[1], Victim::Chunks { file_id: 1, chunks, .. } if chunks == &vec![4, 3])
        );
    }

    #[test]
    fn headers_last_out() {
        let mut ix = Index::new();
        ix.set_meta(1, meta(1));
        ix.set_meta(2, meta(1));
        ix.set_header(1, 4 << 20);
        ix.touch_chunk(2, 7, 4 << 20);
        ix.set_header(2, 4 << 20);
        // Chunk first, even though header 1 is older.
        let v = ix.victims(4 << 20);
        assert!(matches!(&v[..], [Victim::Chunks { file_id: 2, .. }]));
        // Then whole files, oldest header first.
        let v = ix.victims(9 << 20);
        assert!(matches!(
            &v[..],
            [Victim::Chunks { .. }, Victim::File { file_id: 1, .. }, ..]
        ));
        ix.forget(1);
        ix.forget(2);
        assert_eq!((ix.used(), ix.header_used(), ix.files()), (0, 0, 0));
    }

    #[test]
    fn new_version_drops_the_old() {
        let mut ix = Index::new();
        ix.set_meta(1, meta(1));
        ix.touch_chunk(1, 0, 100);
        ix.set_meta(1, meta(2));
        assert_eq!(ix.used(), 0);
        assert!(!ix.has_chunk(1, 0));
    }

    #[test]
    fn remove_and_retry() {
        let mut ix = Index::new();
        ix.set_meta(1, meta(1));
        ix.set_meta(2, meta(1));
        ix.touch_chunk(1, 0, 10);
        ix.touch_chunk(2, 0, 10);
        let v = ix.victims(10);
        ix.retry_later(&v[0]);
        // File 2's chunk is now older.
        assert!(matches!(
            &ix.victims(10)[0],
            Victim::Chunks { file_id: 2, .. }
        ));
        ix.remove_chunks(1, &[0]);
        assert_eq!(ix.used(), 10);
    }
}
