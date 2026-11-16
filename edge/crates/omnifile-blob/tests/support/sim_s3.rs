//! Spawns the sim's S3 layout (`python -m omnifile_service.backends.sim_http
//! --layout s3 --admin`, the S3 track's facade): S3 data plane with SigV4
//! checked, a fake `aws_signing_helper serve` under `/latest`, and admin
//! routes under `/_sim`.
//!
//! Skipped (with a note) when `uv` is missing or the facade has no S3
//! layout yet, unless `OMNIFILE_REQUIRE_SIM=1`, which makes either a failure.

#![allow(dead_code)]

use std::io::{BufRead, BufReader};
use std::os::unix::process::CommandExt;
use std::path::PathBuf;
use std::process::{Child, Command, Stdio};

pub struct SimS3 {
    child: Child,
    /// The `--clock offset` file (moved by `/_sim/advance`).
    _clock: tempfile::NamedTempFile,
    pub url: String,
    pub imds_url: String,
    pub region: String,
    pub access_key_id: String,
    pub secret_access_key: String,
}

impl Drop for SimS3 {
    fn drop(&mut self) {
        let pgid = self.child.id() as libc::pid_t;
        // SAFETY: kill has no memory preconditions.
        unsafe {
            libc::kill(-pgid, libc::SIGKILL);
        }
        let _ = self.child.kill();
        let _ = self.child.wait();
    }
}

fn repo_root() -> PathBuf {
    PathBuf::from(env!("CARGO_MANIFEST_DIR"))
        .join("../../..")
        .canonicalize()
        .expect("repo root")
}

fn skip(why: &str) -> Option<SimS3> {
    if std::env::var("OMNIFILE_REQUIRE_SIM").as_deref() == Ok("1") {
        panic!("OMNIFILE_REQUIRE_SIM=1 but {why}");
    }
    eprintln!("skipping: {why}");
    None
}

impl SimS3 {
    pub fn start() -> Option<SimS3> {
        let uv = Command::new("uv")
            .arg("--version")
            .stdout(Stdio::null())
            .stderr(Stdio::null())
            .status()
            .map(|s| s.success())
            .unwrap_or(false);
        if !uv {
            return skip("`uv` is not on PATH, cannot run the sim S3 facade");
        }
        let clock = tempfile::NamedTempFile::new().expect("clock file");
        std::fs::write(clock.path(), "+0").expect("clock file");
        let mut child = Command::new("uv")
            .args(["run", "--quiet", "--project"])
            .arg(repo_root().join("service"))
            .args([
                "python",
                "-m",
                "omnifile_service.backends.sim_http",
                "--port",
                "0",
                "--layout",
                "s3",
                "--admin",
                // The wall clock (credential expiry is real time) that
                // `/_sim/advance` can move, with no skew check.
                "--clock",
                "offset",
                "--clock-file",
            ])
            .arg(clock.path())
            .stdout(Stdio::piped())
            .stderr(Stdio::null())
            .process_group(0)
            .spawn()
            .expect("spawn uv");
        let stdout = child.stdout.take().expect("stdout");
        let mut line = String::new();
        let _ = BufReader::new(stdout).read_line(&mut line);
        let v: serde_json::Value = match serde_json::from_str(&line) {
            Ok(v) => v,
            Err(_) => {
                let _ = child.kill();
                let _ = child.wait();
                return skip(
                    "the sim facade did not start with `--layout s3` (no S3 layout in this tree?)",
                );
            }
        };
        if v["layout"].as_str() != Some("s3") {
            let _ = child.kill();
            return skip("the sim facade did not start the S3 layout");
        }
        let s = |k: &str| v[k].as_str().unwrap_or_default().to_owned();
        Some(SimS3 {
            url: s("url"),
            imds_url: s("imds_url"),
            region: s("region"),
            access_key_id: s("access_key_id"),
            secret_access_key: s("secret_access_key"),
            child,
            _clock: clock,
        })
    }

    /// `host:port` of the facade.
    pub fn host_port(&self) -> String {
        self.url
            .trim_start_matches("http://")
            .trim_end_matches('/')
            .to_owned()
    }

    pub async fn admin(&self, path: &str, body: serde_json::Value) -> serde_json::Value {
        let resp = super::sim::plain_http()
            .post(format!("{}{path}", self.url))
            .header("content-type", "application/json")
            .body(body.to_string())
            .send()
            .await
            .expect("sim admin");
        let status = resp.status();
        let text = resp.text().await.unwrap();
        assert!(status.is_success(), "{path}: {status} {text}");
        serde_json::from_str(&text).unwrap_or(serde_json::Value::Null)
    }

    pub async fn get(&self, path: &str) -> serde_json::Value {
        let resp = super::sim::plain_http()
            .get(format!("{}{path}", self.url))
            .send()
            .await
            .expect("sim admin");
        serde_json::from_str(&resp.text().await.unwrap()).unwrap()
    }
}

/// The sim's synthetic payload: byte `i` is `(seed mod 251 + i) mod 251`.
pub fn synthetic(seed: u64, offset: u64, len: usize) -> Vec<u8> {
    let s = seed % 251;
    (0..len as u64)
        .map(|i| ((s + offset + i) % 251) as u8)
        .collect()
}
