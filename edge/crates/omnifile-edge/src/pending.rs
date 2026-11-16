//! The durable log of pending closes (WP9).
//!
//! A close the write detector has read is otherwise only in memory: while it
//! waits out the quiet period, then in the commit queue, then in the commit
//! itself. A crash or restart in between lost the write. For a new file the
//! namespace sweep would find it again, but not for an overwrite of a file
//! already committed: the catalog went on serving the old version, and the
//! new bytes had no copy off the box. So the detector records each file it
//! starts waiting on here, before it does anything else with the event, and
//! the commit pipeline marks it done once the commit has an outcome. At start
//! the daemon replays what is still pending into the new detector, which
//! waits out a quiet period and reports it as if its close had just been
//! seen. The fd store covers closes not read yet; this covers the ones read.
//!
//! The log is one append-only text file next to the replica
//! (`<replica dir>/pending-closes`), one record per line:
//!
//! ```text
//! + <token> <ino> <generation>     a file is waited on
//! - <token>                        its commit is done
//! ```
//!
//! A file seen again while it is still pending gets a new token in memory
//! only (no record): a commit done for the old token then leaves it pending.
//! Additions are made durable (`fdatasync`) once per batch of events;
//! removals are not, since a lost one only costs a check at the next start
//! (the commit finds the file unchanged and does nothing). The file is
//! rewritten with only the pending records at open, and whenever it has
//! grown well past them.

use std::collections::HashMap;
use std::fs::{File, OpenOptions};
use std::io::{self, Write};
use std::path::{Path, PathBuf};
use std::sync::Mutex;

use tracing::warn;

/// `(ino, generation)`.
pub type Key = (u64, u32);

pub struct PendingCloses {
    inner: Mutex<Inner>,
}

struct Inner {
    path: PathBuf,
    file: File,
    /// Pending files: the newest token, and the token their `+` record on
    /// disk carries (what a `-` must name).
    live: HashMap<Key, (u64, u64)>,
    by_token: HashMap<u64, Key>,
    next: u64,
    /// Records written since the last rewrite.
    records: usize,
    /// Additions not yet made durable.
    unsynced: bool,
}

/// Parse a log: what is still pending, and the highest token seen.
pub fn parse(text: &str) -> (HashMap<u64, Key>, u64) {
    let mut live: HashMap<u64, Key> = HashMap::new();
    let mut max = 0;
    for line in text.lines() {
        let mut w = line.split_ascii_whitespace();
        match (w.next(), w.next().and_then(|t| t.parse::<u64>().ok())) {
            (Some("+"), Some(t)) => {
                let ino = w.next().and_then(|v| v.parse().ok());
                let generation = w.next().and_then(|v| v.parse().ok());
                if let (Some(ino), Some(g)) = (ino, generation) {
                    live.insert(t, (ino, g));
                    max = max.max(t);
                }
            }
            (Some("-"), Some(t)) => {
                live.remove(&t);
                max = max.max(t);
            }
            // A line cut short by a crash, or anything else: skipped.
            _ => {}
        }
    }
    (live, max)
}

fn write_all(path: &Path, live: &HashMap<Key, (u64, u64)>) -> io::Result<File> {
    let tmp = path.with_extension("new");
    {
        let mut f = File::create(&tmp)?;
        let mut buf = String::new();
        let mut rows: Vec<(&u64, &Key)> = live.iter().map(|(k, (_, t))| (t, k)).collect();
        rows.sort();
        for (t, (ino, g)) in rows {
            buf.push_str(&format!("+ {t} {ino} {g}\n"));
        }
        f.write_all(buf.as_bytes())?;
        f.sync_all()?;
    }
    std::fs::rename(&tmp, path)?;
    if let Some(dir) = path.parent()
        && let Ok(d) = File::open(dir)
    {
        let _ = d.sync_all();
    }
    OpenOptions::new().append(true).open(path)
}

impl PendingCloses {
    /// The log for a replica at `replica_path`.
    pub fn path_for(replica_path: &Path) -> PathBuf {
        replica_path.with_file_name("pending-closes")
    }

    /// Open (or create) the log at `path`: returns it and what is pending,
    /// as `(token, (ino, generation))`.
    pub fn open(path: &Path) -> io::Result<(PendingCloses, Vec<(u64, Key)>)> {
        let text = match std::fs::read(path) {
            Ok(b) => String::from_utf8_lossy(&b).into_owned(),
            Err(e) if e.kind() == io::ErrorKind::NotFound => String::new(),
            Err(e) => return Err(e),
        };
        let (by_token, max) = parse(&text);
        // One token per inode: the newest.
        let mut live: HashMap<Key, (u64, u64)> = HashMap::new();
        for (t, k) in &by_token {
            let e = live.entry(*k).or_insert((*t, *t));
            if *t > e.0 {
                *e = (*t, *t);
            }
        }
        let by_token: HashMap<u64, Key> = live.iter().map(|(k, (t, _))| (*t, *k)).collect();
        // The rewrite only compacts: if it fails, the pending entries are
        // still returned and replayed, and the log is appended to as it is.
        let file = match write_all(path, &live) {
            Ok(f) => f,
            Err(e) => {
                warn!(path = %path.display(), err = %e,
                      "pending-close log: could not rewrite it; appending to it as it is");
                OpenOptions::new().append(true).create(true).open(path)?
            }
        };
        let mut pending: Vec<(u64, Key)> = by_token.iter().map(|(t, k)| (*t, *k)).collect();
        pending.sort();
        let records = live.len();
        Ok((
            PendingCloses {
                inner: Mutex::new(Inner {
                    path: path.to_path_buf(),
                    file,
                    live,
                    by_token,
                    next: max + 1,
                    records,
                    unsynced: false,
                }),
            },
            pending,
        ))
    }

    /// `key` is waited on: its token. Recorded unless it already was.
    pub fn observed(&self, key: Key) -> u64 {
        let mut st = self.inner.lock().unwrap();
        let t = st.next;
        st.next += 1;
        match st.live.get(&key).copied() {
            Some((old, disk)) => {
                // Still pending: a newer close of the same file. A commit
                // done for the older one leaves it pending.
                st.by_token.remove(&old);
                st.by_token.insert(t, key);
                st.live.insert(key, (t, disk));
            }
            None => {
                st.by_token.insert(t, key);
                st.live.insert(key, (t, t));
                let line = format!("+ {t} {} {}\n", key.0, key.1);
                if let Err(e) = st.file.write_all(line.as_bytes()) {
                    warn!(err = %e, "pending-close log: could not record a write");
                }
                st.records += 1;
                st.unsynced = true;
            }
        }
        t
    }

    /// Make the recorded additions durable.
    pub fn sync(&self) {
        let mut st = self.inner.lock().unwrap();
        if st.unsynced {
            st.unsynced = false;
            if let Err(e) = st.file.sync_data() {
                warn!(err = %e, "pending-close log: fdatasync");
            }
        }
    }

    /// The commit for `token` has an outcome: no longer pending, unless the
    /// file was seen again since (it then has a newer token).
    pub fn done(&self, token: u64) {
        self.done_with(token, false)
    }

    /// [`PendingCloses::done`], and the record made durable at once: for an
    /// upload now held in the outbox, so a host crash can't replay the close
    /// into a second upload of the same write (a permanent duplicate on a
    /// WORM share).
    pub fn done_durable(&self, token: u64) {
        self.done_with(token, true)
    }

    fn done_with(&self, token: u64, durable: bool) {
        let mut st = self.inner.lock().unwrap();
        let Some(key) = st.by_token.remove(&token) else {
            return;
        };
        let disk = match st.live.get(&key) {
            Some((newest, disk)) if *newest == token => *disk,
            // Seen again since: still pending, under its newer token.
            _ => return,
        };
        st.live.remove(&key);
        let line = format!("- {disk}\n");
        if let Err(e) = st.file.write_all(line.as_bytes()) {
            warn!(err = %e, "pending-close log: could not record a commit");
        }
        if durable && let Err(e) = st.file.sync_data() {
            warn!(err = %e, "pending-close log: fdatasync");
        }
        st.records += 1;
        if st.records > 2 * st.live.len() + 10_000 {
            let path = st.path.clone();
            match write_all(&path, &st.live) {
                Ok(f) => {
                    st.file = f;
                    st.records = st.live.len();
                    st.unsynced = false;
                }
                Err(e) => warn!(err = %e, "pending-close log: rewrite"),
            }
        }
    }

    /// How many files are pending.
    pub fn len(&self) -> usize {
        self.inner.lock().unwrap().live.len()
    }

    pub fn is_empty(&self) -> bool {
        self.len() == 0
    }
}

/// The detector's side: the generation comes from the open file.
#[cfg(target_os = "linux")]
impl omnifile_detect::PendingLog for PendingCloses {
    fn observed(&self, file: &File) -> u64 {
        use std::os::fd::AsFd;
        use std::os::unix::fs::MetadataExt;
        let ino = file.metadata().map(|m| m.ino()).unwrap_or(0);
        let g = omnifile_stub::sys::inode_generation(file.as_fd()).unwrap_or(0);
        PendingCloses::observed(self, (ino, g))
    }

    fn sync(&self) {
        PendingCloses::sync(self)
    }

    fn finished(&self, token: u64) {
        PendingCloses::done(self, token)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn pending_survives_reopen_and_done_removes_it() {
        let d = tempfile::tempdir().unwrap();
        let p = d.path().join("pending-closes");
        let (log, pending) = PendingCloses::open(&p).unwrap();
        assert!(pending.is_empty());
        let a = log.observed((10, 1));
        let b = log.observed((11, 7));
        log.sync();
        log.done(a);
        drop(log);
        let (log, pending) = PendingCloses::open(&p).unwrap();
        assert_eq!(pending, vec![(b, (11, 7))]);
        // Tokens never repeat across opens.
        assert!(log.observed((12, 1)) > b);
    }

    #[test]
    fn a_file_seen_again_stays_pending_after_the_older_commit() {
        let d = tempfile::tempdir().unwrap();
        let p = d.path().join("pending-closes");
        let (log, _) = PendingCloses::open(&p).unwrap();
        let first = log.observed((5, 2));
        let second = log.observed((5, 2));
        log.done(first);
        assert_eq!(log.len(), 1, "the newer close is still pending");
        drop(log);
        let (log, pending) = PendingCloses::open(&p).unwrap();
        assert_eq!(pending.len(), 1);
        assert_eq!(pending[0].1, (5, 2));
        log.done(pending[0].0);
        assert!(log.is_empty());
        let _ = second;
    }

    /// WP9 review (finding 5): a rewrite that fails at open (here its
    /// temporary name is taken by a directory) doesn't stop the replay.
    #[test]
    fn a_failed_rewrite_still_returns_what_is_pending() {
        let d = tempfile::tempdir().unwrap();
        let p = d.path().join("pending-closes");
        std::fs::write(&p, "+ 4 40 1\n+ 5 50 2\n- 4\n").unwrap();
        std::fs::create_dir(p.with_extension("new")).unwrap();
        let (log, pending) = PendingCloses::open(&p).unwrap();
        assert_eq!(pending, vec![(5, (50, 2))]);
        // Still recorded to, by appending.
        let t = log.observed((60, 3));
        log.sync();
        log.done(5);
        drop(log);
        std::fs::remove_dir(p.with_extension("new")).unwrap();
        let (_, pending) = PendingCloses::open(&p).unwrap();
        assert_eq!(pending, vec![(t, (60, 3))]);
    }

    #[test]
    fn a_line_cut_short_by_a_crash_is_skipped() {
        let (live, max) = parse("+ 1 10 1\n+ 2 11 3\n- 1\n+ 3 1");
        assert_eq!(live, HashMap::from([(2, (11, 3))]));
        assert_eq!(max, 2);
    }
}
