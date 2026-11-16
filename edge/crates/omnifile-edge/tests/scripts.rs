//! Every script a workflow runs directly (not through `bash` or `python3`)
//! is executable in git, or the lane dies with "Permission denied" (Codex on
//! #28: appliance/release.sh was committed 100644).

use std::os::unix::fs::PermissionsExt;
use std::path::{Path, PathBuf};

fn repo() -> PathBuf {
    PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../../..")
}

/// The scripts a shell line runs directly: words naming a repo `.sh`/`.py`
/// file that are not the argument of an interpreter or `source`.
fn direct_scripts(line: &str, root: &Path) -> Vec<String> {
    let words: Vec<&str> = line
        .split(|c: char| {
            c.is_whitespace() || c == '(' || c == ')' || c == ';' || c == '&' || c == '|'
        })
        .filter(|w| !w.is_empty())
        .collect();
    let mut out = Vec::new();
    for (i, w) in words.iter().enumerate() {
        let w = w.trim_matches(|c| c == '"' || c == '\'');
        if !(w.ends_with(".sh") || w.ends_with(".py")) || w.contains('$') || w.starts_with('-') {
            continue;
        }
        if !root.join(w).is_file() {
            continue;
        }
        let prev = if i > 0 { words[i - 1] } else { "" };
        let interpreted = [
            "bash",
            "sh",
            "python3",
            "python",
            ".",
            "source",
            "-c",
            "shellcheck",
        ]
        .contains(&prev)
            || prev.ends_with("python3");
        if !interpreted {
            out.push(w.to_string());
        }
    }
    out
}

#[test]
fn scripts_the_workflows_run_directly_are_executable() {
    let root = repo();
    let mut checked = Vec::new();
    let mut bad = Vec::new();
    for e in std::fs::read_dir(root.join(".github/workflows")).unwrap() {
        let p = e.unwrap().path();
        let text = std::fs::read_to_string(&p).unwrap();
        for line in text.lines() {
            let line = line.trim_start().trim_start_matches("run:").trim();
            if line.starts_with('#') {
                continue;
            }
            for s in direct_scripts(line, &root) {
                let mode = std::fs::metadata(root.join(&s))
                    .unwrap()
                    .permissions()
                    .mode();
                if mode & 0o111 == 0 {
                    bad.push(format!(
                        "{s} (run by {})",
                        p.file_name().unwrap().to_string_lossy()
                    ));
                }
                checked.push(s);
            }
        }
    }
    assert!(
        checked.iter().any(|s| s == "appliance/release.sh"),
        "the workflows run appliance/release.sh directly: {checked:?}"
    );
    assert!(bad.is_empty(), "not executable: {bad:?}");
}

#[test]
fn the_scan_finds_direct_runs_and_skips_interpreted_ones() {
    let root = repo();
    assert_eq!(
        direct_scripts("sudo -E appliance/release.sh --out x", &root),
        vec!["appliance/release.sh"]
    );
    assert!(direct_scripts("sudo bash appliance/tests/vm.sh", &root).is_empty());
    assert!(direct_scripts("python3 appliance/tests/ra-stub.py 1 x", &root).is_empty());
    assert_eq!(
        direct_scripts("IMAGE=$IMAGE dev/scripts/build-artifacts.sh", &root),
        vec!["dev/scripts/build-artifacts.sh"]
    );
}
