//! Spawns the sim Azure Blob HTTP facade
//! (`python -m omnifile_service.backends.sim_http`) for integration tests.
//!
//! Needs `uv` on PATH and the service project at `<repo>/service`. Without
//! `uv` the tests are skipped (they print why), unless
//! `OMNIFILE_REQUIRE_SIM=1`, which turns the skip into a failure.

#![allow(dead_code)]

use std::io::{BufRead, BufReader};
use std::os::unix::process::CommandExt;
use std::path::PathBuf;
use std::process::{Child, Command, Stdio};

pub struct Sim {
    child: Child,
    pub url: String,
}

impl Drop for Sim {
    fn drop(&mut self) {
        // `uv run` starts python as its own child: kill the whole group, or
        // the orphaned server keeps the test's stderr open.
        let pgid = self.child.id() as libc::pid_t;
        unsafe {
            libc::kill(-pgid, libc::SIGKILL);
        }
        let _ = self.child.kill();
        let _ = self.child.wait();
    }
}

fn repo_root() -> PathBuf {
    // edge/crates/<crate> -> repo root
    PathBuf::from(env!("CARGO_MANIFEST_DIR"))
        .join("../../..")
        .canonicalize()
        .expect("repo root")
}

fn have_uv() -> bool {
    Command::new("uv")
        .arg("--version")
        .stdout(Stdio::null())
        .stderr(Stdio::null())
        .status()
        .map(|s| s.success())
        .unwrap_or(false)
}

impl Sim {
    /// Start the facade, or `None` (skip) when `uv` is unavailable.
    pub fn start() -> Option<Sim> {
        if !have_uv() {
            if std::env::var("OMNIFILE_REQUIRE_SIM").as_deref() == Ok("1") {
                panic!("OMNIFILE_REQUIRE_SIM=1 but `uv` is not on PATH");
            }
            eprintln!("skipping: `uv` not on PATH, cannot run the sim Blob facade");
            return None;
        }
        let service = repo_root().join("service");
        let mut child = Command::new("uv")
            .args(["run", "--quiet", "--project"])
            .arg(&service)
            .args([
                "python",
                "-m",
                "omnifile_service.backends.sim_http",
                "--port",
                "0",
                "--layout",
                "two-account",
                // `POST /_sim/sas` mints the test's SAS URLs (admin is opt-in).
                "--admin",
            ])
            .stdout(Stdio::piped())
            .stderr(Stdio::inherit())
            .process_group(0)
            .spawn()
            .expect("spawn uv");
        let stdout = child.stdout.take().expect("stdout");
        let mut line = String::new();
        BufReader::new(stdout)
            .read_line(&mut line)
            .expect("read sim banner");
        let v: serde_json::Value =
            serde_json::from_str(&line).unwrap_or_else(|e| panic!("sim banner {line:?}: {e}"));
        let url = v["url"].as_str().expect("url").to_owned();
        Some(Sim { child, url })
    }

    /// Mint a SAS URL through `POST /_sim/sas`.
    pub async fn sas(&self, container: &str, blob: &str, perms: &str, ttl_s: u64) -> String {
        let body = serde_json::json!({
            "account": "live", "container": container, "blob": blob,
            "permissions": perms, "ttl_seconds": ttl_s,
        });
        let resp = plain_http()
            .post(format!("{}/_sim/sas", self.url))
            .header("content-type", "application/json")
            .body(body.to_string())
            .send()
            .await
            .expect("mint sas");
        assert!(resp.status().is_success(), "mint sas: {}", resp.status());
        let v: serde_json::Value = serde_json::from_str(&resp.text().await.unwrap()).unwrap();
        v["url"].as_str().unwrap().to_owned()
    }
}

/// A reqwest client usable for plain-HTTP test calls (the crate's TLS
/// backend has no process-wide provider, so `reqwest::Client::new()` would not do).
pub fn plain_http() -> reqwest::Client {
    let cfg = omnifile_blob::TlsOptions::public()
        .client_config()
        .expect("tls");
    omnifile_blob::tls::http_client(cfg, std::time::Duration::from_secs(5)).expect("client")
}
