//! The read-ahead planner: detects sequential reads per file and asks for
//! chunks ahead of them, in a window that doubles while the reads stay
//! sequential (up to a cap) and closes on a random read. Random access
//! (seismic trace picking) gets no read-ahead, so it costs no egress.

use std::collections::HashMap;

#[derive(Clone, Copy, Debug)]
struct Stream {
    /// Where a sequential reader reads next.
    next: u64,
    /// Chunks of read-ahead now.
    window: u64,
    /// Read-ahead was already asked for every chunk below this one.
    ahead_to: u64,
}

/// Per-file sequential-read detection. Bounded: the oldest streams are
/// dropped past `max_files`.
pub struct Planner {
    max_chunks: u64,
    max_files: usize,
    streams: HashMap<u64, Stream>,
}

impl Planner {
    pub fn new(max_chunks: u32, max_files: usize) -> Self {
        Self {
            max_chunks: u64::from(max_chunks),
            max_files: max_files.max(1),
            streams: HashMap::new(),
        }
    }

    /// A read of `[offset, offset+len)` of a file of `size` with chunks of
    /// `1 << shift`. Returns the chunks to read ahead (beyond the read's own).
    pub fn observe(
        &mut self,
        file_id: u64,
        offset: u64,
        len: u64,
        size: u64,
        shift: u8,
    ) -> std::ops::Range<u64> {
        let end = offset.saturating_add(len).min(size);
        if self.max_chunks == 0 || end <= offset {
            return 0..0;
        }
        if !self.streams.contains_key(&file_id) && self.streams.len() >= self.max_files {
            // Forget an arbitrary stream: a lost stream only costs read-ahead.
            if let Some(k) = self.streams.keys().next().copied() {
                self.streams.remove(&k);
            }
        }
        let chunk = 1u64 << shift;
        let s = self.streams.entry(file_id).or_insert(Stream {
            next: u64::MAX,
            window: 0,
            ahead_to: 0,
        });
        // Sequential: starts where the last read ended, or within the chunk
        // before it (NFS clients reorder and overlap a little).
        let sequential = offset == s.next
            || (s.next != u64::MAX && offset < s.next && s.next - offset <= chunk && end > s.next);
        if sequential {
            s.window = (s.window * 2).clamp(2, self.max_chunks);
        } else if offset == 0 {
            s.window = 1.min(self.max_chunks);
        } else {
            s.window = 0;
            s.ahead_to = 0;
        }
        s.next = end;
        let last = ChunkCount::of(size, shift);
        let first_ahead = ((end - 1) >> shift) + 1;
        let start = first_ahead.max(s.ahead_to);
        let stop = (first_ahead + s.window).min(last);
        if start >= stop {
            return 0..0;
        }
        s.ahead_to = stop;
        start..stop
    }

    pub fn forget(&mut self, file_id: u64) {
        self.streams.remove(&file_id);
    }
}

struct ChunkCount;
impl ChunkCount {
    fn of(size: u64, shift: u8) -> u64 {
        if size == 0 {
            0
        } else {
            ((size - 1) >> shift) + 1
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    const S: u8 = 22;
    const C: u64 = 1 << 22;

    #[test]
    fn sequential_reads_open_the_window() {
        let mut p = Planner::new(16, 100);
        let size = 1 << 30;
        assert_eq!(p.observe(1, 0, 1 << 20, size, S), 1..2);
        assert_eq!(p.observe(1, 1 << 20, 1 << 20, size, S), 2..3); // window 2: 1..3, 1 done
        let r = p.observe(1, 2 << 20, 1 << 20, size, S);
        assert_eq!(r, 3..5); // window 4
        let r = p.observe(1, 3 << 20, 1 << 20, size, S);
        assert_eq!(r, 5..9); // window 8
        let mut off = 4 << 20;
        let mut last = 0..0;
        for _ in 0..10 {
            last = p.observe(1, off, C, size, S);
            off += C;
        }
        assert!(last.end - ((off - 1) >> S) <= 17, "capped: {last:?}");
    }

    #[test]
    fn random_reads_get_none() {
        let mut p = Planner::new(16, 100);
        let size = 100 << 30;
        for off in [7 * C + 5, 900 * C, 3 * C, 40_000 * C] {
            assert_eq!(p.observe(1, off, 4096, size, S), 0..0);
        }
    }

    #[test]
    fn stops_at_eof_and_is_bounded() {
        let mut p = Planner::new(16, 2);
        let size = 3 * C;
        assert_eq!(p.observe(1, 0, C, size, S), 1..2);
        assert_eq!(p.observe(1, C, C, size, S), 2..3);
        assert_eq!(p.observe(1, 2 * C, C, size, S), 0..0);
        p.observe(2, 0, 1, size, S);
        p.observe(3, 0, 1, size, S);
        assert!(p.streams.len() <= 2);
        assert_eq!(Planner::new(0, 1).observe(1, 0, C, size, S), 0..0);
    }
}
