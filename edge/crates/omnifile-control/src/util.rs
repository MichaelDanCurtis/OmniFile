//! Small helpers: RFC 3339 times, backoff, idempotency keys, atomic files.

use std::io::Write;
use std::path::Path;
use std::time::{Duration, SystemTime};

use time::OffsetDateTime;
use time::format_description::well_known::Rfc3339;

use crate::error::{ControlError, Result};

/// Parse an RFC 3339 time.
pub fn parse_time(s: &str) -> Option<SystemTime> {
    OffsetDateTime::parse(s, &Rfc3339)
        .ok()
        .map(SystemTime::from)
}

/// Format as RFC 3339 UTC.
pub fn format_time(t: SystemTime) -> String {
    OffsetDateTime::from(t)
        .format(&Rfc3339)
        .expect("RFC 3339 formatting of a SystemTime")
}

/// Jittered exponential backoff, 1 s up to 5 min by default (control API §9).
#[derive(Clone, Debug)]
pub struct Backoff {
    pub base: Duration,
    pub max: Duration,
    attempt: u32,
}

impl Default for Backoff {
    fn default() -> Self {
        Self::new(Duration::from_secs(1), Duration::from_secs(300))
    }
}

impl Backoff {
    pub fn new(base: Duration, max: Duration) -> Self {
        Self {
            base,
            max,
            attempt: 0,
        }
    }

    /// The next delay: full jitter over `[d/2, d]`, `d = base × 2^n`, at
    /// least `floor` (a server `Retry-After`) but never above `max`.
    pub fn next_delay(&mut self, floor: Option<Duration>) -> Duration {
        let d = self
            .base
            .saturating_mul(1u32 << self.attempt.min(20))
            .min(self.max);
        self.attempt = self.attempt.saturating_add(1);
        d.mul_f64(0.5 + fastrand::f64() * 0.5)
            .max(floor.unwrap_or_default())
            .min(self.max)
    }

    pub fn reset(&mut self) {
        self.attempt = 0;
    }

    pub fn attempts(&self) -> u32 {
        self.attempt
    }
}

/// A fresh idempotency key: a UUIDv7 (36 characters).
pub fn new_idempotency_key() -> String {
    uuid::Uuid::now_v7().to_string()
}

/// A deterministic idempotency key for a natural key (an `op_id`, an
/// `upload_id`), so a retry after a restart reuses it: 40 hex digits of
/// SHA-256(`kind` NUL `natural`). Always within the 16–128 limit.
pub fn derived_idempotency_key(kind: &str, natural: &str) -> String {
    let mut ctx = ring::digest::Context::new(&ring::digest::SHA256);
    ctx.update(kind.as_bytes());
    ctx.update(&[0]);
    ctx.update(natural.as_bytes());
    hex(&ctx.finish().as_ref()[..20])
}

pub fn sha256_hex(data: &[u8]) -> String {
    hex(ring::digest::digest(&ring::digest::SHA256, data).as_ref())
}

pub fn hex(bytes: &[u8]) -> String {
    use std::fmt::Write as _;
    let mut s = String::with_capacity(bytes.len() * 2);
    for b in bytes {
        let _ = write!(s, "{b:02x}");
    }
    s
}

/// Write `data` to `path` atomically, with exactly `mode` on Unix: a new
/// temp file with an unpredictable name (`O_EXCL`, so a planted file or
/// symlink at that name is never followed or reused), `fchmod` to `mode`
/// whatever the umask, `fsync`, rename, then `fsync` the directory.
pub fn write_atomic(path: &Path, data: &[u8], mode: u32) -> Result<()> {
    use std::os::unix::fs::{OpenOptionsExt, PermissionsExt};
    let dir = path
        .parent()
        .ok_or_else(|| ControlError::Invalid(format!("{} has no parent", path.display())))?;
    std::fs::create_dir_all(dir)?;
    let name = path.file_name().and_then(|n| n.to_str()).unwrap_or("f");
    let mut tmp = None;
    for _ in 0..8 {
        let mut r = [0u8; 12];
        ring::rand::SecureRandom::fill(&ring::rand::SystemRandom::new(), &mut r)
            .map_err(|_| ControlError::Invalid("no randomness for a temp name".into()))?;
        let p = dir.join(format!(".{name}.tmp-{}", hex(&r)));
        match std::fs::OpenOptions::new()
            .write(true)
            // O_CREAT | O_EXCL: never an existing file or symlink.
            .create_new(true)
            .mode(mode & 0o7777)
            .open(&p)
        {
            Ok(f) => {
                tmp = Some((p, f));
                break;
            }
            Err(e) if e.kind() == std::io::ErrorKind::AlreadyExists => continue,
            Err(e) => return Err(e.into()),
        }
    }
    let (tmp, mut f) =
        tmp.ok_or_else(|| ControlError::Invalid("could not create a temp file".into()))?;
    let res = (|| -> std::io::Result<()> {
        // The create mode is masked by the umask: set it explicitly.
        f.set_permissions(std::fs::Permissions::from_mode(mode & 0o7777))?;
        f.write_all(data)?;
        f.sync_all()?;
        std::fs::rename(&tmp, path)
    })();
    if let Err(e) = res {
        let _ = std::fs::remove_file(&tmp);
        return Err(e.into());
    }
    if let Ok(d) = std::fs::File::open(dir) {
        let _ = d.sync_all();
    }
    Ok(())
}

/// `s` with the value of every `sig=` query parameter (a SAS signature)
/// replaced, for anything stored or logged.
pub fn redact_sas(s: &str) -> String {
    let mut out = s.to_owned();
    // Azure SAS `sig=`; S3 presigned URLs' signature and session token.
    for marker in ["sig=", "X-Amz-Signature=", "X-Amz-Security-Token="] {
        out = redact_after(&out, marker);
    }
    out
}

fn redact_after(s: &str, marker: &str) -> String {
    let mut out = String::with_capacity(s.len());
    let mut rest = s;
    while let Some(i) = rest.find(marker) {
        out.push_str(&rest[..i + marker.len()]);
        out.push_str("REDACTED");
        let tail = &rest[i + marker.len()..];
        let end = tail
            .find(|c: char| c == '&' || c == '"' || c == '\'' || c == ')' || c.is_whitespace())
            .unwrap_or(tail.len());
        rest = &tail[end..];
    }
    out.push_str(rest);
    out
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn redact_sas_hides_azure_and_s3_signatures() {
        let s = redact_sas(
            "GET https://a/c/b?sv=1&sig=AZSECRET&x=1 and \
             https://k.s3/x?X-Amz-Security-Token=TOKSECRET&X-Amz-Signature=S3SECRET",
        );
        for secret in ["AZSECRET", "TOKSECRET", "S3SECRET"] {
            assert!(!s.contains(secret), "{s}");
        }
        assert!(s.contains("sig=REDACTED&x=1") && s.contains("X-Amz-Signature=REDACTED"));
    }

    #[test]
    fn write_atomic_sets_the_mode_and_never_follows_a_planted_name() {
        use std::os::unix::fs::PermissionsExt;
        let d = tempfile::tempdir().unwrap();
        let p = d.path().join("key.pem");
        // Whatever the umask, the file ends up exactly 0600.
        // SAFETY: umask has no preconditions; restored below.
        let old = unsafe { libc::umask(0) };
        let r = write_atomic(&p, b"secret", 0o600);
        // SAFETY: as above.
        unsafe { libc::umask(old) };
        r.unwrap();
        assert_eq!(
            std::fs::metadata(&p).unwrap().permissions().mode() & 0o777,
            0o600
        );
        // The old predictable temp name, planted as a symlink, is not used.
        let victim = d.path().join("victim");
        std::fs::write(&victim, b"untouched").unwrap();
        let planted = d
            .path()
            .join(format!(".key.pem.tmp-{}", std::process::id()));
        std::os::unix::fs::symlink(&victim, &planted).unwrap();
        write_atomic(&p, b"secret2", 0o600).unwrap();
        assert_eq!(std::fs::read(&victim).unwrap(), b"untouched");
        assert_eq!(std::fs::read(&p).unwrap(), b"secret2");
        // No temp file is left behind.
        let left: Vec<_> = std::fs::read_dir(d.path())
            .unwrap()
            .filter_map(|e| e.ok())
            .filter(|e| e.file_name().to_string_lossy().contains(".tmp-"))
            .map(|e| e.file_name())
            .collect();
        assert_eq!(left.len(), 1, "only the planted link: {left:?}");
    }

    #[test]
    fn sas_signatures_are_redacted() {
        assert_eq!(
            redact_sas("GET https://a/c/b?sv=1&sig=abc%2Fdef&se=2 failed"),
            "GET https://a/c/b?sv=1&sig=REDACTED&se=2 failed"
        );
        assert_eq!(redact_sas("x sig=zz"), "x sig=REDACTED");
        assert_eq!(redact_sas("nothing here"), "nothing here");
    }

    #[test]
    fn time_round_trip() {
        let t = parse_time("2026-10-05T12:00:00Z").unwrap();
        assert_eq!(format_time(t), "2026-10-05T12:00:00Z");
        assert!(parse_time("yesterday").is_none());
    }

    #[test]
    fn backoff_grows_and_caps() {
        let mut b = Backoff::default();
        let first = b.next_delay(None);
        assert!(first >= Duration::from_millis(500) && first <= Duration::from_secs(1));
        for _ in 0..30 {
            assert!(b.next_delay(None) <= Duration::from_secs(300));
        }
        b.reset();
        assert!(b.next_delay(Some(Duration::from_secs(7))) >= Duration::from_secs(7));
    }

    #[test]
    fn idempotency_keys() {
        let k = new_idempotency_key();
        assert!((16..=128).contains(&k.len()));
        let a = derived_idempotency_key("upload", "edge-1:42");
        assert_eq!(a, derived_idempotency_key("upload", "edge-1:42"));
        assert_ne!(a, derived_idempotency_key("complete", "edge-1:42"));
        assert_eq!(a.len(), 40);
    }
}
