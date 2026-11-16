//! Copying one NAS file to Azure (`annex.md` §3.1–3.3), through the same
//! upload path as the edge's own files: `uploads` (intent), Put Block with
//! per-block `x-ms-content-crc64`, Put Block List with OmniFile's metadata
//! names and no tier, then `uploads/complete`. The service's `verify` queue
//! then reads the blob back in Azure (decision 24).
//!
//! The NAS is never written, and the bytes are read twice, never spooled:
//! 1. **Hash pass**: BLAKE3 and the per-block CRC-64/NVME of the file as it
//!    is now. The file is stat'ed before and after with
//!    `AT_STATX_FORCE_SYNC` (the NAS's attributes, not the client's cached
//!    ones); if anything changed while it was read, the copy is thrown away
//!    and the file waits for its next quiet period. A read error that isn't
//!    a change is tried [`READ_ATTEMPTS`] times, then the copy fails
//!    (`copy_failed`), never retried forever.
//! 2. **Upload pass**: the file's cached pages are dropped first
//!    (`POSIX_FADV_DONTNEED`), so the second read comes from the NAS, not
//!    the hash pass's cache. Every block read for upload is checked against
//!    the hash pass's CRC, so a file that changes during the upload is
//!    caught before Azure keeps a byte of it (the upload fails with an
//!    integrity error and the file goes back to waiting).
//! 3. **Re-stat after reading**, forced from the NAS, once every block is
//!    staged and before Put Block List: if the file changed, nothing is
//!    committed in Azure (no blob that nothing refers to, which would be
//!    billed, and undeletable under version-level WORM), the pending version
//!    is discarded (`uploads/abort`) and no `copied` is sent (`annex.md`
//!    §2.2); the file is copied after its next quiet period.
//!
//! **Crash safety.** The copy is journalled (`copy_job`) before the intent is
//! sent. Its op id is made of the catalog file and the NAS attributes, so a
//! restarted unit sends the same intent: the service answers with the same
//! pending upload (the block journal resumes it) or, if it was already
//! completed, `dedupe` with the committed version. A copy is never
//! committed twice, and none is lost.

#[cfg(test)]
use std::path::Path;
use std::path::PathBuf;

use omnifile_blob::{CommitProps, PosixMeta, UploadRequest};
use omnifile_control::types as ct;
use omnifile_integrity::{BlockPlan, IntegrityHasher};
use serde_json::json;

use super::db::{AnnexDb, CopyJob, Entry, Seen};
use super::nasfs::{Root, drop_cache, open_root};
use super::scan::{gone_error, rfc3339_ms};
use super::service::{AnnexService, Start, SvcError};

/// How many times a hash pass that fails with a read error (not a change)
/// is tried before the copy fails.
pub const READ_ATTEMPTS: u32 = 3;

/// How one copy ended.
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum Copied {
    /// Committed as `version_id` (now, or before a restart).
    Done { version_id: u64 },
    /// The file changed while it was read, or since it was reported: it
    /// waits for its next quiet period.
    Changed,
    /// Gone from the NAS before it could be copied.
    Gone,
    /// The service is unreachable: try again later, nothing lost.
    Later(String),
    /// Refused or unreadable (`copy_failed`, with the reason).
    Failed(String),
}

pub struct Copier<'a, S: AnnexService> {
    pub svc: &'a S,
    pub db: &'a AnnexDb,
    pub annex_id: u64,
    /// The mountpoint, and the annex's path beneath it (`nasfs::open_root`).
    pub root: PathBuf,
    pub root_rel: String,
    /// Upload block journals (resume after a restart).
    pub journals: PathBuf,
    pub now: i64,
    /// Tests only: an error to fail each hash pass's read with.
    pub read_fault: Option<fn(&str) -> Option<std::io::Error>>,
}

type Look = Option<(Seen, u32, u32, u32)>;

/// [`stat`] on a blocking thread: a forced stat of a network file can wait
/// on the NAS, and must not hold up the runtime.
async fn stat_off(fs: &Root, rel: &str) -> std::io::Result<Look> {
    let fs = fs.try_clone()?;
    let rel = rel.to_owned();
    tokio::task::spawn_blocking(move || stat(&fs, &rel))
        .await
        .map_err(std::io::Error::other)?
}

/// A file's attributes from the NAS (forced past the client's cache), with
/// its owner, group and mode; `None` if it is no longer a file there.
fn stat(fs: &Root, rel: &str) -> std::io::Result<Look> {
    match fs.stat(rel, true) {
        Ok(m) if m.is_file() => Ok(Some((m.seen(), m.uid, m.gid, m.mode))),
        Ok(_) => Ok(None),
        Err(e) if gone_error(&e) => Ok(None),
        Err(e) => Err(e),
    }
}

/// The op id of one copy: the catalog file and the NAS attributes it was
/// read with. Same file, same bytes: same op id (control API §4.2: `uploads`
/// answers a known op id with its upload, or `dedupe` once committed).
pub fn op_id(annex_id: u64, file_id: u64, s: &Seen) -> String {
    format!(
        "annex:{annex_id}:{file_id}:{}:{}:{}:{}",
        s.ino, s.size, s.mtime_ns, s.ctime_ns
    )
}

/// Read the whole file once: BLAKE3, CRC-64/NVME, per-block CRCs, and its
/// first 4 KB (the rules' content type, sniffed from a read the copy makes
/// anyway, so no other pass moves the file's atime).
fn hash(
    f: &mut std::fs::File,
    size: u64,
) -> std::io::Result<(omnifile_integrity::FileDigest, Vec<u8>)> {
    use std::io::Read;
    let plan = BlockPlan::new(size)
        .map_err(|e| std::io::Error::new(std::io::ErrorKind::InvalidInput, format!("{e:?}")))?;
    let mut h = IntegrityHasher::with_plan(plan, true);
    let mut buf = vec![0u8; 8 << 20];
    let mut total = 0u64;
    let mut head = Vec::new();
    loop {
        let n = f.read(&mut buf)?;
        if n == 0 {
            break;
        }
        if head.len() < 4096 {
            let k = (4096 - head.len()).min(n);
            head.extend_from_slice(&buf[..k]);
        }
        total += n as u64;
        if total > size {
            return Err(std::io::Error::other("the file grew while it was read"));
        }
        h.update(&buf[..n])
            .map_err(|e| std::io::Error::other(e.to_string()))?;
    }
    let d = h
        .finish()
        .map_err(|e| std::io::Error::other(e.to_string()))?;
    Ok((d, head))
}

impl<'a, S: AnnexService> Copier<'a, S> {
    fn journal_path(&self, op: &str) -> PathBuf {
        let h = omnifile_integrity::crc64_nvme(op.as_bytes());
        self.journals.join(format!("{h:016x}.journal"))
    }

    /// Copy `e` (state `ready`, with its catalog `file_id`).
    pub async fn copy(&self, e: &Entry) -> Copied {
        self.copy_with(e, None).await
    }

    /// Copy `e` again under a new op id (its earlier copy no longer matches,
    /// so it must not be answered with that copy).
    pub async fn copy_again(&self, e: &Entry) -> Copied {
        self.copy_with(e, Some(format!("r{}", e.problem_at.unwrap_or(0))))
            .await
    }

    async fn copy_with(&self, e: &Entry, nonce: Option<String>) -> Copied {
        let Some(file_id) = e.file_id else {
            return Copied::Failed("no catalog file yet".into());
        };
        let fs = match open_root(&self.root, &self.root_rel) {
            Ok(r) => r,
            Err(err) => return Copied::Later(format!("the annex root: {err}")),
        };
        let before = match stat_off(&fs, &e.path).await {
            Ok(Some(s)) => s,
            Ok(None) => return Copied::Gone,
            Err(err) => return Copied::Failed(format!("stat: {err}")),
        };
        let (seen, uid, gid, mode) = before.clone();
        if let Some((rs, rm, rc)) = e.reported
            && !seen.same(rs, rm, rc)
        {
            return Copied::Changed; // changed since it was reported
        }
        let mut op = op_id(self.annex_id, file_id, &seen);
        if let Some(n) = &nonce {
            op = format!("{op}:{n}");
        }
        let job = CopyJob {
            path: e.path.clone(),
            file_id,
            op_id: op.clone(),
            size: seen.size,
            mtime_ns: seen.mtime_ns,
            ctime_ns: seen.ctime_ns,
            upload_id: None,
            version_id: None,
        };
        if let Err(err) = self.db.start_copy(&job, self.now) {
            return Copied::Later(format!("annex.db: {err}"));
        }
        // 1. The hash pass, bracketed by stats from the NAS.
        let size = seen.size;
        let mut attempt = 0u32;
        let digest = loop {
            attempt += 1;
            let f = match fs.open_file(&e.path) {
                Ok(f) => f,
                Err(err) if gone_error(&err) => return Copied::Gone,
                Err(err) if err.kind() == std::io::ErrorKind::PermissionDenied => {
                    return Copied::Failed(format!("unreadable: {err}"));
                }
                Err(err) => return Copied::Failed(format!("open: {err}")),
            };
            let fault = self.read_fault.and_then(|f| f(&e.path));
            let res = tokio::task::spawn_blocking(move || {
                let mut f = f;
                let r = match fault {
                    Some(err) => Err(err),
                    None => hash(&mut f, size),
                };
                // The upload pass reads from the NAS again, not this cache.
                drop_cache(&f);
                r
            })
            .await;
            let err = match res {
                Ok(Ok((d, head))) => {
                    // Cached for the age rules (and any later preview).
                    let t = super::rules::sniff(&head);
                    let _ = self.db.set_content_type(&e.path, t, seen.mtime_ns);
                    break d;
                }
                Ok(Err(err)) => err,
                Err(err) => return Copied::Later(format!("hash task: {err}")),
            };
            if err.kind() == std::io::ErrorKind::NotFound {
                return Copied::Gone;
            }
            if err.kind() == std::io::ErrorKind::PermissionDenied {
                return Copied::Failed(format!("unreadable: {err}"));
            }
            // A change (it grew, or was replaced) waits for its quiet period;
            // a read error on a file that holds still is tried again, then
            // fails.
            match stat_off(&fs, &e.path).await {
                Ok(Some((now_seen, ..))) if now_seen != seen => return Copied::Changed,
                Ok(None) => return Copied::Gone,
                _ => {}
            }
            if attempt >= READ_ATTEMPTS {
                return Copied::Failed(format!("read error after {attempt} attempts: {err}"));
            }
            tokio::time::sleep(std::time::Duration::from_millis(200 * attempt as u64)).await;
        };
        match stat_off(&fs, &e.path).await {
            Ok(Some((now_seen, ..))) if now_seen == seen => {}
            Ok(Some(_)) => return Copied::Changed,
            Ok(None) => return Copied::Gone,
            Err(err) => return Copied::Failed(format!("stat: {err}")),
        }
        let block_crcs: Vec<u64> = digest.blocks.iter().map(|b| b.crc64).collect();
        let intent = ct::UploadIntent {
            op_id: op.clone(),
            file: ct::FileRef::Id { file_id },
            size: seen.size,
            mtime_ns: seen.mtime_ns,
            blake3: digest.blake3.to_hex().to_string(),
            crc64nvme: digest.crc64,
            block_size: digest.plan.block_size(),
            base_version_id: e.content_version,
        };
        let start = match self.svc.upload_start(&intent).await {
            Ok(s) => s,
            Err(err) if err.code.as_deref() == Some("version_conflict") => {
                // The catalog moved on (another copy committed): ask again.
                let mut i2 = intent.clone();
                i2.base_version_id = None;
                match self.svc.upload_start(&i2).await {
                    Ok(s) => s,
                    Err(err) => return self.failed(err),
                }
            }
            Err(err) => return self.failed(err),
        };
        let version_id = match start {
            Start::Dedupe { version_id } => version_id,
            Start::Upload {
                upload_id,
                version_id,
                sas_url,
            } => {
                let _ = self.db.copy_upload(&e.path, upload_id, version_id);
                // The upload pass reads the file opened beneath the root
                // (never its path resolved again), from the NAS.
                let src = match fs.open_file(&e.path) {
                    Ok(f) => f,
                    Err(err) if gone_error(&err) => {
                        let _ = self.svc.upload_abort(upload_id).await;
                        let _ = self.db.end_copy(&e.path);
                        return Copied::Gone;
                    }
                    Err(err) => return Copied::Failed(format!("open: {err}")),
                };
                drop_cache(&src);
                let req = UploadRequest {
                    sas_url,
                    source: fs.source_path(&e.path, &src),
                    plan: digest.plan,
                    // 2. Every block re-read is checked against the hash pass.
                    expected_block_crcs: Some(block_crcs),
                    journal: self.journal_path(&op),
                    fingerprint: op.clone(),
                    props: CommitProps {
                        metadata: PosixMeta { uid, gid, mode }.to_metadata(),
                        // Never a tier: Smart accounts (AGENTS.md).
                        access_tier: None,
                        content_type: None,
                    },
                };
                // 3. The last look, from the NAS, after every block is staged
                // and before the block list is committed: a change since the
                // hash pass stops the upload with nothing committed in Azure.
                let gate = || {
                    let (fs, path, seen) = (&fs, e.path.clone(), seen.clone());
                    let f: std::pin::Pin<
                        Box<dyn std::future::Future<Output = Result<(), String>> + Send + '_>,
                    > = Box::pin(async move {
                        match stat_off(fs, &path).await {
                            Ok(Some((s, ..))) if s == seen => Ok(()),
                            Ok(Some(_)) => Err("the file changed since the hash pass".to_owned()),
                            Ok(None) => Err("the file is gone".to_owned()),
                            Err(err) => Err(format!("stat: {err}")),
                        }
                    });
                    f
                };
                let out = match self.svc.upload_blob(&req, &gate).await {
                    Ok(o) => o,
                    Err(err) if err.code.as_deref() == Some("integrity") => {
                        let _ = self.svc.upload_abort(upload_id).await;
                        let _ = std::fs::remove_file(&req.journal);
                        let _ = self.db.end_copy(&e.path);
                        return Copied::Changed;
                    }
                    Err(err) => return self.failed(err),
                };
                drop(src);
                if out.crc64nvme != digest.crc64 || out.size != seen.size {
                    let _ = self.svc.upload_abort(upload_id).await;
                    let _ = std::fs::remove_file(&req.journal);
                    let _ = self.db.end_copy(&e.path);
                    return Copied::Changed;
                }
                let body = ct::UploadComplete {
                    crc64nvme: out.crc64nvme,
                    blake3: digest.blake3.to_hex().to_string(),
                    size: out.size,
                    etag: out.etag.clone(),
                    backend_version_id: out.version_id.clone(),
                    blocks: out
                        .blocks
                        .iter()
                        .map(|b| ct::CompleteBlock {
                            id: b.id.clone(),
                            length: b.len,
                            crc64nvme: b.crc64nvme,
                        })
                        .collect(),
                };
                let v = match self.svc.upload_complete(upload_id, &body).await {
                    Ok(v) => v,
                    Err(err) => return self.failed(err),
                };
                let _ = std::fs::remove_file(&req.journal);
                v
            }
        };
        // The copy is recorded as of the attributes it was read with; a later
        // change is a new version, after its own quiet period.
        let event = json!({
            "kind": "copied", "path": e.path, "file_id": file_id.to_string(),
            "version_id": version_id.to_string(), "size": seen.size,
            "mtime_ns": seen.mtime_ns.to_string(), "ctime_ns": seen.ctime_ns.to_string(),
            "observed_at": rfc3339_ms(self.now),
        });
        let r = self
            .db
            .push_event("copied", Some(&e.path), &event)
            .and_then(|_| {
                self.db
                    .set_copied(&e.path, seen.size, seen.mtime_ns, seen.ctime_ns, version_id)
            });
        if let Err(err) = r {
            return Copied::Later(format!("annex.db: {err}"));
        }
        let _ = self.db.end_copy(&e.path);
        // A change since the last look: copied again later.
        if let Ok(Some((after, ..))) = stat_off(&fs, &e.path).await
            && after != seen
        {
            let _ = self.db.observe(&e.path, &after, self.now, false, 0);
        }
        Copied::Done { version_id }
    }

    fn failed(&self, err: SvcError) -> Copied {
        if err.unavailable {
            Copied::Later(err.to_string())
        } else {
            Copied::Failed(err.to_string())
        }
    }
}

#[cfg(test)]
mod tests {
    use super::super::db::AnnexDb;
    use super::super::scan::Scanner;
    use super::super::service::Fake;
    use super::*;

    fn ready(db: &AnnexDb, path: &str, file_id: u64) -> Entry {
        let e = db.entry(path).unwrap().unwrap();
        db.set_reported(path, &e.seen).unwrap();
        db.set_catalog(path, file_id, None, true).unwrap();
        db.entry(path).unwrap().unwrap()
    }

    #[tokio::test]
    async fn a_file_is_copied_once_and_a_restart_never_commits_twice() {
        let t = tempfile::tempdir().unwrap();
        let j = tempfile::tempdir().unwrap();
        std::fs::write(t.path().join("f.bin"), vec![7u8; 100_000]).unwrap();
        let db = AnnexDb::open_in_memory().unwrap();
        Scanner::new(&db, t.path(), 1, false).full().unwrap();
        let e = ready(&db, "f.bin", 1);
        let svc = Fake::default();
        let c = Copier {
            svc: &svc,
            db: &db,
            annex_id: 3,
            root: t.path().into(),
            root_rel: String::new(),
            journals: j.path().into(),
            now: 10,
            read_fault: None,
        };
        let Copied::Done { version_id } = c.copy(&e).await else {
            panic!("not copied")
        };
        assert_eq!(svc.state.lock().unwrap().commits, 1);
        assert_eq!(
            svc.state.lock().unwrap().blobs[&version_id],
            vec![7u8; 100_000]
        );
        assert_eq!(db.entry("f.bin").unwrap().unwrap().state, "copied");
        // Killed before the `copied` event was recorded: the same intent
        // again is a dedupe of the committed version, not a second commit.
        db.set_state("f.bin", "ready").unwrap();
        let again = c.copy(&db.entry("f.bin").unwrap().unwrap()).await;
        assert_eq!(again, Copied::Done { version_id });
        assert_eq!(svc.state.lock().unwrap().commits, 1);
    }

    #[tokio::test]
    async fn a_file_changed_since_it_was_reported_waits() {
        let t = tempfile::tempdir().unwrap();
        let j = tempfile::tempdir().unwrap();
        std::fs::write(t.path().join("f"), b"one").unwrap();
        let db = AnnexDb::open_in_memory().unwrap();
        Scanner::new(&db, t.path(), 1, false).full().unwrap();
        let e = ready(&db, "f", 1);
        std::thread::sleep(std::time::Duration::from_millis(20));
        std::fs::write(t.path().join("f"), b"two, longer").unwrap();
        let svc = Fake::default();
        let c = Copier {
            svc: &svc,
            db: &db,
            annex_id: 3,
            root: t.path().into(),
            root_rel: String::new(),
            journals: j.path().into(),
            now: 10,
            read_fault: None,
        };
        assert_eq!(c.copy(&e).await, Copied::Changed);
        assert_eq!(svc.state.lock().unwrap().commits, 0);
    }

    #[tokio::test]
    async fn an_unreachable_service_loses_nothing() {
        let t = tempfile::tempdir().unwrap();
        let j = tempfile::tempdir().unwrap();
        std::fs::write(t.path().join("f"), b"bytes").unwrap();
        let db = AnnexDb::open_in_memory().unwrap();
        Scanner::new(&db, t.path(), 1, false).full().unwrap();
        let e = ready(&db, "f", 1);
        let svc = Fake::with(|s| s.fail_blob = true);
        let c = Copier {
            svc: &svc,
            db: &db,
            annex_id: 3,
            root: t.path().into(),
            root_rel: String::new(),
            journals: j.path().into(),
            now: 10,
            read_fault: None,
        };
        assert!(matches!(c.copy(&e).await, Copied::Later(_)));
        assert_eq!(db.entry("f").unwrap().unwrap().state, "ready");
        assert!(matches!(c.copy(&e).await, Copied::Done { .. }));
        assert_eq!(svc.state.lock().unwrap().commits, 1);
    }

    fn copier<'a>(svc: &'a Fake, db: &'a AnnexDb, root: &Path, j: &Path) -> Copier<'a, Fake> {
        Copier {
            svc,
            db,
            annex_id: 3,
            root: root.into(),
            root_rel: String::new(),
            journals: j.into(),
            now: 10,
            read_fault: None,
        }
    }

    #[tokio::test]
    async fn a_change_after_the_upload_pass_discards_the_version() {
        let t = tempfile::tempdir().unwrap();
        let j = tempfile::tempdir().unwrap();
        let f = t.path().join("f.bin");
        std::fs::write(&f, vec![1u8; 50_000]).unwrap();
        let db = AnnexDb::open_in_memory().unwrap();
        Scanner::new(&db, t.path(), 1, false).full().unwrap();
        let e = ready(&db, "f.bin", 1);
        let svc = Fake::default();
        let f2 = f.clone();
        svc.state.lock().unwrap().after_blob = Some(Box::new(move || {
            std::thread::sleep(std::time::Duration::from_millis(20));
            std::fs::write(&f2, vec![2u8; 50_000]).unwrap();
        }));
        let c = copier(&svc, &db, t.path(), j.path());
        assert_eq!(c.copy(&e).await, Copied::Changed);
        let st = svc.state.lock().unwrap();
        assert_eq!(st.commits, 0, "never completed");
        assert!(st.blobs.is_empty(), "no block list committed");
        assert_eq!(
            st.orphans(),
            Vec::<u64>::new(),
            "no blob that nothing refers to"
        );
        assert_eq!(st.aborted.len(), 1, "the pending version is discarded");
        drop(st);
        assert!(
            db.outbox(100).unwrap().iter().all(|q| q.1 != "copied"),
            "no `copied` for a version that isn't what the file is"
        );
    }

    #[tokio::test]
    async fn a_persistent_read_error_fails_the_copy_after_bounded_tries() {
        let t = tempfile::tempdir().unwrap();
        let j = tempfile::tempdir().unwrap();
        std::fs::write(t.path().join("f"), b"bytes").unwrap();
        let db = AnnexDb::open_in_memory().unwrap();
        Scanner::new(&db, t.path(), 1, false).full().unwrap();
        let e = ready(&db, "f", 1);
        let svc = Fake::default();
        let mut c = copier(&svc, &db, t.path(), j.path());
        c.read_fault = Some(|_| Some(std::io::Error::from_raw_os_error(libc::EIO)));
        match c.copy(&e).await {
            Copied::Failed(why) => assert!(why.contains("after 3 attempts"), "{why}"),
            other => panic!("{other:?}: a read error that isn't a change must fail, not wait"),
        }
        assert_eq!(svc.state.lock().unwrap().commits, 0);
    }

    #[tokio::test]
    async fn a_folder_swapped_for_a_symlink_is_never_followed() {
        let t = tempfile::tempdir().unwrap();
        let out = tempfile::tempdir().unwrap();
        let j = tempfile::tempdir().unwrap();
        std::fs::create_dir(t.path().join("d")).unwrap();
        std::fs::write(t.path().join("d/f"), b"the NAS's").unwrap();
        let db = AnnexDb::open_in_memory().unwrap();
        Scanner::new(&db, t.path(), 1, false).full().unwrap();
        let e = ready(&db, "d/f", 1);
        // Same name, same size, outside the annex.
        std::fs::write(out.path().join("f"), b"secret!!!").unwrap();
        std::fs::remove_dir_all(t.path().join("d")).unwrap();
        std::os::unix::fs::symlink(out.path(), t.path().join("d")).unwrap();
        let svc = Fake::default();
        let c = copier(&svc, &db, t.path(), j.path());
        assert_eq!(c.copy(&e).await, Copied::Gone);
        assert!(svc.state.lock().unwrap().blobs.is_empty());
    }
}
