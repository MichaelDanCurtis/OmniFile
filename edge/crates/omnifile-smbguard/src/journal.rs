//! The event journal (recommended design item 5).
//!
//! The kernel matches a response to a pending permission event by the fd
//! number alone (`process_access_response()`), so a restarted guard can
//! answer the events its predecessor read but did not answer, if it knows
//! them. Every batch of events is journalled in **one `write()` before any
//! of it is acted on**; every answer appends an end record.
//!
//! Format: text lines, so an operator can read it.
//!
//! ```text
//! G <group-id>                                   first line: which group the journal belongs to
//! H <fd>                                         highest event fd number ever journalled for the
//!                                                group (written on every rewrite, so compaction
//!                                                never shrinks the restart sweep's bound)
//! B <fd> <ino> <gen> <off> <len> <pid> <handle>  an event was read (handle = <type>:<hex>, or -)
//! E <fd>                                         that event was answered
//! ```
//!
//! The journal lives on `/run` (tmpfs). No `fsync`: pending events do not
//! survive a reboot either, and a reboot also discards the fd store. A
//! journal whose `G` line names another group (the store was lost and a new
//! group created) is ignored on replay.
//!
//! It is compacted when it grows past a limit: the still-pending records
//! are written to a new file, which is renamed over the old one.

use std::collections::{BTreeMap, HashMap};
use std::fs::{File, OpenOptions};
use std::io::{self, BufRead, BufReader, Write};
use std::path::{Path, PathBuf};
use std::sync::Mutex;

/// A kernel file handle (`struct file_handle`), as returned by
/// `name_to_handle_at`.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Handle {
    pub handle_type: i32,
    pub bytes: Vec<u8>,
}

impl Handle {
    pub fn encode(&self) -> String {
        let mut s = format!("{}:", self.handle_type);
        for b in &self.bytes {
            s.push_str(&format!("{b:02x}"));
        }
        s
    }

    pub fn decode(s: &str) -> Option<Handle> {
        let (t, hex) = s.split_once(':')?;
        if hex.len() % 2 != 0 || hex.len() > 256 {
            return None;
        }
        let bytes = (0..hex.len() / 2)
            .map(|i| u8::from_str_radix(&hex[2 * i..2 * i + 2], 16).ok())
            .collect::<Option<Vec<u8>>>()?;
        Some(Handle {
            handle_type: t.parse().ok()?,
            bytes,
        })
    }
}

/// One journalled event.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Record {
    pub fd: i32,
    pub ino: u64,
    pub ino_gen: u32,
    pub off: u64,
    pub len: u64,
    pub pid: i32,
    pub handle: Option<Handle>,
}

impl Record {
    fn line(&self) -> String {
        format!(
            "B {} {} {} {} {} {} {}\n",
            self.fd,
            self.ino,
            self.ino_gen,
            self.off,
            self.len,
            self.pid,
            self.handle
                .as_ref()
                .map(Handle::encode)
                .unwrap_or_else(|| "-".into())
        )
    }

    fn parse(p: &[&str]) -> Option<Record> {
        let [fd, ino, ino_gen, off, len, pid, h] = p else {
            return None;
        };
        Some(Record {
            fd: fd.parse().ok()?,
            ino: ino.parse().ok()?,
            ino_gen: ino_gen.parse().ok()?,
            off: off.parse().ok()?,
            len: len.parse().ok()?,
            pid: pid.parse().ok()?,
            handle: if *h == "-" { None } else { Handle::decode(h) },
        })
    }
}

/// What a previous instance left behind.
#[derive(Debug, Default, PartialEq, Eq)]
pub struct Replay {
    /// The journal names the group we hold.
    pub same_group: bool,
    /// Events read but not answered, by fd number.
    pub pending: BTreeMap<i32, Record>,
    /// Highest fd number ever journalled (for the deny-sweep bound).
    pub max_fd: i32,
}

/// Read a journal. A missing file is an empty replay.
pub fn replay(path: &Path, group_id: &str) -> io::Result<Replay> {
    let f = match File::open(path) {
        Ok(f) => f,
        Err(e) if e.kind() == io::ErrorKind::NotFound => return Ok(Replay::default()),
        Err(e) => return Err(e),
    };
    let mut r = Replay::default();
    for (i, line) in BufReader::new(f).lines().enumerate() {
        let line = line?;
        let p: Vec<&str> = line.split_whitespace().collect();
        match p.as_slice() {
            ["G", id] if i == 0 => r.same_group = *id == group_id,
            ["H", fd] => {
                if let Ok(fd) = fd.parse::<i32>() {
                    r.max_fd = r.max_fd.max(fd);
                }
            }
            ["B", rest @ ..] => {
                if let Some(rec) = Record::parse(rest) {
                    r.max_fd = r.max_fd.max(rec.fd);
                    r.pending.insert(rec.fd, rec);
                }
            }
            ["E", fd] => {
                if let Ok(fd) = fd.parse::<i32>() {
                    r.pending.remove(&fd);
                }
            }
            // A torn last line (crash mid-write) or garbage: skip it. An
            // event lost this way is caught by the deny-sweep.
            _ => {}
        }
    }
    if !r.same_group {
        r.pending.clear();
    }
    Ok(r)
}

struct Inner {
    file: File,
    bytes: u64,
    pending: HashMap<i32, String>,
    /// Highest event fd number journalled for this group, across instances.
    high_water: i32,
}

/// The live journal.
pub struct Journal {
    path: PathBuf,
    group_id: String,
    max_bytes: u64,
    inner: Mutex<Inner>,
}

impl Journal {
    /// Start a fresh journal for `group_id`, carrying `carry` (the
    /// predecessor's still-pending events) so a second crash does not lose
    /// them. `high_water` is the predecessor's [`Replay::max_fd`] (0 for a
    /// new group). Written to a temporary file and renamed into place.
    pub fn create(
        path: &Path,
        group_id: &str,
        carry: &[Record],
        high_water: i32,
        max_bytes: u64,
    ) -> io::Result<Journal> {
        let pending: HashMap<i32, String> = carry.iter().map(|r| (r.fd, r.line())).collect();
        let high_water = carry.iter().map(|r| r.fd).fold(high_water.max(0), i32::max);
        let (file, bytes) = write_fresh(path, group_id, high_water, pending.values())?;
        Ok(Journal {
            path: path.to_path_buf(),
            group_id: group_id.to_string(),
            max_bytes,
            inner: Mutex::new(Inner {
                file,
                bytes,
                pending,
                high_water,
            }),
        })
    }

    /// Journal a batch of events in one `write()`.
    pub fn begin(&self, recs: &[Record]) -> io::Result<()> {
        if recs.is_empty() {
            return Ok(());
        }
        let mut buf = String::new();
        let mut g = self.inner.lock().unwrap();
        for r in recs {
            let l = r.line();
            buf.push_str(&l);
            g.pending.insert(r.fd, l);
            g.high_water = g.high_water.max(r.fd);
        }
        g.file.write_all(buf.as_bytes())?;
        g.bytes += buf.len() as u64;
        Ok(())
    }

    /// Record that these events were answered.
    pub fn end(&self, fds: &[i32]) -> io::Result<()> {
        if fds.is_empty() {
            return Ok(());
        }
        let mut buf = String::new();
        let mut g = self.inner.lock().unwrap();
        for fd in fds {
            buf.push_str(&format!("E {fd}\n"));
            g.pending.remove(fd);
        }
        g.file.write_all(buf.as_bytes())?;
        g.bytes += buf.len() as u64;
        if g.bytes > self.max_bytes {
            let (file, bytes) =
                write_fresh(&self.path, &self.group_id, g.high_water, g.pending.values())?;
            g.file = file;
            g.bytes = bytes;
        }
        Ok(())
    }

    /// Events journalled and not yet answered.
    pub fn pending(&self) -> usize {
        self.inner.lock().unwrap().pending.len()
    }
}

fn write_fresh<'a>(
    path: &Path,
    group_id: &str,
    high_water: i32,
    lines: impl Iterator<Item = &'a String>,
) -> io::Result<(File, u64)> {
    let tmp = path.with_extension("new");
    let mut f = OpenOptions::new()
        .create(true)
        .write(true)
        .truncate(true)
        .open(&tmp)?;
    let mut buf = format!("G {group_id}\nH {high_water}\n");
    for l in lines {
        buf.push_str(l);
    }
    f.write_all(buf.as_bytes())?;
    std::fs::rename(&tmp, path)?;
    let f = OpenOptions::new().append(true).open(path)?;
    Ok((f, buf.len() as u64))
}

#[cfg(test)]
mod tests {
    use super::*;

    fn rec(fd: i32) -> Record {
        Record {
            fd,
            ino: 1000 + fd as u64,
            ino_gen: 7,
            off: 4096,
            len: 8192,
            pid: 42,
            handle: Some(Handle {
                handle_type: 0x81,
                bytes: vec![1, 2, 3, 0xff],
            }),
        }
    }

    #[test]
    fn handle_round_trip() {
        let h = Handle {
            handle_type: 0x81,
            bytes: vec![0, 1, 0xab],
        };
        assert_eq!(Handle::decode(&h.encode()), Some(h));
        assert_eq!(Handle::decode("1:abc"), None);
        assert_eq!(Handle::decode("x"), None);
    }

    #[test]
    fn unanswered_events_replay() {
        let d = tempfile::tempdir().unwrap();
        let p = d.path().join("journal");
        let j = Journal::create(&p, "g1", &[], 0, 1 << 20).unwrap();
        j.begin(&[rec(5), rec(6), rec(7)]).unwrap();
        j.end(&[6]).unwrap();
        // fd 5 answered, then reused by a new event that is still pending.
        j.end(&[5]).unwrap();
        j.begin(&[rec(5)]).unwrap();
        let r = replay(&p, "g1").unwrap();
        assert!(r.same_group);
        assert_eq!(r.pending.keys().copied().collect::<Vec<_>>(), vec![5, 7]);
        assert_eq!(r.pending[&7], rec(7));
        assert_eq!(r.max_fd, 7);
    }

    #[test]
    fn another_groups_journal_is_ignored() {
        let d = tempfile::tempdir().unwrap();
        let p = d.path().join("journal");
        let j = Journal::create(&p, "old", &[], 0, 1 << 20).unwrap();
        j.begin(&[rec(5)]).unwrap();
        let r = replay(&p, "new").unwrap();
        assert!(!r.same_group);
        assert!(r.pending.is_empty());
    }

    #[test]
    fn carried_records_survive_a_second_crash() {
        let d = tempfile::tempdir().unwrap();
        let p = d.path().join("journal");
        let _j = Journal::create(&p, "g", &[rec(9)], 0, 1 << 20).unwrap();
        let r = replay(&p, "g").unwrap();
        assert_eq!(r.pending.len(), 1);
        assert_eq!(r.pending[&9], rec(9));
    }

    #[test]
    fn compaction_keeps_only_pending() {
        let d = tempfile::tempdir().unwrap();
        let p = d.path().join("journal");
        let j = Journal::create(&p, "g", &[], 0, 2048).unwrap();
        j.begin(&[rec(3)]).unwrap();
        for i in 0..200 {
            j.begin(&[rec(10 + i)]).unwrap();
            j.end(&[10 + i]).unwrap();
        }
        let size = std::fs::metadata(&p).unwrap().len();
        assert!(size < 4096, "journal not compacted: {size} bytes");
        let r = replay(&p, "g").unwrap();
        assert_eq!(r.pending.keys().copied().collect::<Vec<_>>(), vec![3]);
        assert_eq!(j.pending(), 1);
    }

    #[test]
    fn high_water_survives_compaction_and_restarts() {
        let d = tempfile::tempdir().unwrap();
        let p = d.path().join("journal");
        let j = Journal::create(&p, "g", &[], 0, 512).unwrap();
        // A burst reaches fd 900, all answered; compaction then drops every record.
        for fd in 100..=900 {
            j.begin(&[rec(fd)]).unwrap();
            j.end(&[fd]).unwrap();
        }
        j.begin(&[rec(12)]).unwrap();
        let r = replay(&p, "g").unwrap();
        assert_eq!(r.max_fd, 900, "the sweep bound must cover fd 900");
        // The next instance carries it forward, and compacts again.
        let j2 = Journal::create(&p, "g", &[], r.max_fd, 256).unwrap();
        for fd in 20..60 {
            j2.begin(&[rec(fd)]).unwrap();
            j2.end(&[fd]).unwrap();
        }
        assert_eq!(replay(&p, "g").unwrap().max_fd, 900);
    }

    #[test]
    fn torn_last_line_is_skipped() {
        let d = tempfile::tempdir().unwrap();
        let p = d.path().join("journal");
        std::fs::write(&p, "G g\nB 5 1 2 3 4 5 -\nB 6 1 2").unwrap();
        let r = replay(&p, "g").unwrap();
        assert_eq!(r.pending.keys().copied().collect::<Vec<_>>(), vec![5]);
        assert_eq!(r.pending[&5].handle, None);
    }

    #[test]
    fn missing_journal_is_empty() {
        let d = tempfile::tempdir().unwrap();
        let r = replay(&d.path().join("none"), "g").unwrap();
        assert_eq!(r, Replay::default());
    }
}
