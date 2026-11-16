//! Running `wbinfo` / `smbstatus` as separate processes, with a hard timeout.

use std::io::Read;
use std::process::{Command, Stdio};
use std::time::{Duration, Instant};

#[derive(Debug, thiserror::Error)]
pub enum CmdError {
    #[error("{prog}: could not start: {source}")]
    Spawn {
        prog: String,
        source: std::io::Error,
    },
    #[error("{prog}: timed out after {0:?}", .timeout)]
    Timeout { prog: String, timeout: Duration },
    #[error("{prog}: exit status {code:?}: {stderr}")]
    Failed {
        prog: String,
        code: Option<i32>,
        stderr: String,
    },
}

/// Runs `prog args…` with no stdin, captures stdout (and up to 4 KiB of
/// stderr), and kills it if it runs longer than `timeout`. A non-zero exit is
/// [`CmdError::Failed`]. The environment is cleared except `PATH` and `LANG=C`,
/// so output is never localised.
pub fn run_bounded(prog: &str, args: &[&str], timeout: Duration) -> Result<String, CmdError> {
    let mut child = Command::new(prog)
        .args(args)
        .env_clear()
        .env(
            "PATH",
            "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
        )
        .env("LANG", "C")
        .env("LC_ALL", "C")
        .stdin(Stdio::null())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
        .map_err(|source| CmdError::Spawn {
            prog: prog.to_string(),
            source,
        })?;
    let mut out = child.stdout.take().expect("piped");
    let mut err = child.stderr.take().expect("piped");
    let out_t = std::thread::spawn(move || {
        let mut v = Vec::new();
        let _ = out.read_to_end(&mut v);
        v
    });
    let err_t = std::thread::spawn(move || {
        let mut v = Vec::new();
        let _ = (&mut err).take(4096).read_to_end(&mut v);
        // Drain the rest so the child never blocks on a full pipe.
        let _ = std::io::copy(&mut err, &mut std::io::sink());
        v
    });
    let deadline = Instant::now() + timeout;
    let status = loop {
        match child.try_wait() {
            Ok(Some(st)) => break st,
            Ok(None) if Instant::now() >= deadline => {
                let _ = child.kill();
                let _ = child.wait();
                return Err(CmdError::Timeout {
                    prog: prog.to_string(),
                    timeout,
                });
            }
            Ok(None) => std::thread::sleep(Duration::from_millis(2)),
            Err(source) => {
                return Err(CmdError::Spawn {
                    prog: prog.to_string(),
                    source,
                });
            }
        }
    };
    let stdout = out_t.join().unwrap_or_default();
    let stderr = err_t.join().unwrap_or_default();
    if !status.success() {
        return Err(CmdError::Failed {
            prog: prog.to_string(),
            code: status.code(),
            stderr: String::from_utf8_lossy(&stderr).trim().to_string(),
        });
    }
    Ok(String::from_utf8_lossy(&stdout).into_owned())
}

#[cfg(all(test, unix))]
mod tests {
    use super::*;

    #[test]
    fn captures_stdout() {
        let s = run_bounded("sh", &["-c", "echo hi"], Duration::from_secs(5)).unwrap();
        assert_eq!(s, "hi\n");
    }

    #[test]
    fn reports_failure_with_stderr() {
        match run_bounded(
            "sh",
            &["-c", "echo bad >&2; exit 3"],
            Duration::from_secs(5),
        ) {
            Err(CmdError::Failed { code, stderr, .. }) => {
                assert_eq!(code, Some(3));
                assert_eq!(stderr, "bad");
            }
            other => panic!("{other:?}"),
        }
    }

    #[test]
    fn kills_on_timeout() {
        let t0 = Instant::now();
        let r = run_bounded("sleep", &["10"], Duration::from_millis(100));
        assert!(matches!(r, Err(CmdError::Timeout { .. })), "{r:?}");
        assert!(t0.elapsed() < Duration::from_secs(5));
    }

    #[test]
    fn missing_program() {
        assert!(matches!(
            run_bounded("/nonexistent/omni-x", &[], Duration::from_secs(1)),
            Err(CmdError::Spawn { .. })
        ));
    }
}
