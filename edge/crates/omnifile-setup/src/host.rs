//! What the steps do to the machine: read and write files, run commands.
//! [`System`] is the real box (or a staging root that only takes files);
//! [`Fake`] records everything, for tests.

use std::collections::BTreeMap;
use std::io::Write;
use std::os::unix::fs::PermissionsExt;
use std::path::{Path, PathBuf};
use std::process::{Command, Stdio};

/// A finished command.
#[derive(Clone, Debug, Default, PartialEq, Eq)]
pub struct Output {
    pub code: i32,
    pub stdout: String,
    pub stderr: String,
}

impl Output {
    pub fn ok(&self) -> bool {
        self.code == 0
    }
}

pub trait Host {
    fn read(&self, path: &str) -> Option<String>;
    fn exists(&self, path: &str) -> bool;
    /// Writes `content` (atomically, creating directories). Returns whether
    /// the file changed.
    fn write(&mut self, path: &str, content: &str, mode: u32) -> std::io::Result<bool>;
    fn remove(&mut self, path: &str) -> std::io::Result<()>;
    /// Runs `argv` with extra environment and optional stdin. Secrets go
    /// through `env` or `stdin`, never `argv` (which `ps` shows).
    fn run(
        &mut self,
        argv: &[&str],
        env: &[(&str, &str)],
        stdin: Option<&str>,
    ) -> std::io::Result<Output>;
    /// Entries of a directory (names only).
    fn list(&self, dir: &str) -> Vec<String>;
    /// Whether commands really run (false: a staging root).
    fn live(&self) -> bool;
}

/// The machine, or a staging root: with a root other than `/`, files are
/// written below it and commands are not run (they answer success, empty).
pub struct System {
    root: PathBuf,
    /// Commands a staging root skipped, for `--root` runs to show.
    pub skipped: Vec<String>,
}

impl System {
    pub fn new(root: impl Into<PathBuf>) -> System {
        System {
            root: root.into(),
            skipped: Vec::new(),
        }
    }

    fn at(&self, path: &str) -> PathBuf {
        if self.root == Path::new("/") {
            PathBuf::from(path)
        } else {
            self.root.join(path.trim_start_matches('/'))
        }
    }
}

impl Host for System {
    fn read(&self, path: &str) -> Option<String> {
        std::fs::read_to_string(self.at(path)).ok()
    }

    fn exists(&self, path: &str) -> bool {
        self.at(path).exists()
    }

    fn write(&mut self, path: &str, content: &str, mode: u32) -> std::io::Result<bool> {
        let p = self.at(path);
        if std::fs::read(&p).ok().as_deref() == Some(content.as_bytes()) {
            let perm = std::fs::metadata(&p)?.permissions().mode() & 0o7777;
            if perm == mode {
                return Ok(false);
            }
        }
        let dir = p.parent().expect("absolute path");
        std::fs::create_dir_all(dir)?;
        let tmp = dir.join(format!(
            ".{}.omnifile-setup",
            p.file_name().unwrap().to_string_lossy()
        ));
        {
            let mut f = std::fs::File::create(&tmp)?;
            f.set_permissions(std::fs::Permissions::from_mode(mode))?;
            f.write_all(content.as_bytes())?;
            f.sync_all()?;
        }
        std::fs::rename(&tmp, &p)?;
        Ok(true)
    }

    fn remove(&mut self, path: &str) -> std::io::Result<()> {
        match std::fs::remove_file(self.at(path)) {
            Err(e) if e.kind() == std::io::ErrorKind::NotFound => Ok(()),
            r => r,
        }
    }

    fn run(
        &mut self,
        argv: &[&str],
        env: &[(&str, &str)],
        stdin: Option<&str>,
    ) -> std::io::Result<Output> {
        if !self.live() {
            self.skipped.push(argv.join(" "));
            return Ok(Output::default());
        }
        let mut c = Command::new(argv[0]);
        c.args(&argv[1..])
            .stdin(if stdin.is_some() {
                Stdio::piped()
            } else {
                Stdio::null()
            })
            .stdout(Stdio::piped())
            .stderr(Stdio::piped());
        for (k, v) in env {
            c.env(k, v);
        }
        let mut child = c
            .spawn()
            .map_err(|e| std::io::Error::new(e.kind(), format!("{}: {e}", argv[0])))?;
        if let Some(input) = stdin {
            let mut w = child.stdin.take().unwrap();
            w.write_all(input.as_bytes())?;
        }
        let out = child.wait_with_output()?;
        Ok(Output {
            code: out.status.code().unwrap_or(-1),
            stdout: String::from_utf8_lossy(&out.stdout).into_owned(),
            stderr: String::from_utf8_lossy(&out.stderr).into_owned(),
        })
    }

    fn list(&self, dir: &str) -> Vec<String> {
        let mut v: Vec<String> = std::fs::read_dir(self.at(dir))
            .map(|rd| {
                rd.filter_map(|e| e.ok())
                    .map(|e| e.file_name().to_string_lossy().into_owned())
                    .collect()
            })
            .unwrap_or_default();
        v.sort();
        v
    }

    fn live(&self) -> bool {
        self.root == Path::new("/")
    }
}

/// A scripted machine: files in memory, commands answered from a table
/// (first matching prefix of the command line), every call recorded.
#[derive(Default)]
pub struct Fake {
    pub files: BTreeMap<String, (String, u32)>,
    pub dirs: BTreeMap<String, Vec<String>>,
    /// (command-line prefix, answer, used once). Unmatched commands succeed, empty.
    pub answers: Vec<(String, Output, bool)>,
    /// (command-line prefix, file it creates), as enrolment creates its identity.
    pub creates: Vec<(String, String)>,
    /// Every command line run, with the names (not values) of its env.
    pub ran: Vec<String>,
    /// The stdin each command got, by position in `ran`.
    pub stdins: Vec<Option<String>>,
    /// Env values, for checking secrets went there and nowhere else.
    pub envs: Vec<Vec<(String, String)>>,
}

impl Fake {
    fn push(&mut self, prefix: &str, code: i32, stdout: &str, once: bool) -> &mut Self {
        self.answers.push((
            prefix.to_string(),
            Output {
                code,
                stdout: stdout.to_string(),
                stderr: if code == 0 {
                    String::new()
                } else {
                    format!("{prefix} failed")
                },
            },
            once,
        ));
        self
    }

    /// Answers every later command starting with `prefix` (the latest answer wins).
    pub fn answer(&mut self, prefix: &str, code: i32, stdout: &str) -> &mut Self {
        self.push(prefix, code, stdout, false)
    }

    /// Answers the next such command only, then falls back to the earlier answers.
    pub fn answer_once(&mut self, prefix: &str, code: i32, stdout: &str) -> &mut Self {
        self.push(prefix, code, stdout, true)
    }

    pub fn file(&self, path: &str) -> Option<&str> {
        self.files.get(path).map(|(c, _)| c.as_str())
    }

    pub fn ran_any(&self, prefix: &str) -> bool {
        self.ran.iter().any(|c| c.starts_with(prefix))
    }
}

impl Host for Fake {
    fn read(&self, path: &str) -> Option<String> {
        self.file(path).map(str::to_string)
    }

    fn exists(&self, path: &str) -> bool {
        self.files.contains_key(path) || self.dirs.contains_key(path)
    }

    fn write(&mut self, path: &str, content: &str, mode: u32) -> std::io::Result<bool> {
        let new = (content.to_string(), mode);
        let changed = self.files.get(path) != Some(&new);
        self.files.insert(path.to_string(), new);
        Ok(changed)
    }

    fn remove(&mut self, path: &str) -> std::io::Result<()> {
        self.files.remove(path);
        Ok(())
    }

    fn run(
        &mut self,
        argv: &[&str],
        env: &[(&str, &str)],
        stdin: Option<&str>,
    ) -> std::io::Result<Output> {
        let line = argv.join(" ");
        self.ran.push(line.clone());
        self.stdins.push(stdin.map(str::to_string));
        self.envs.push(
            env.iter()
                .map(|(k, v)| (k.to_string(), v.to_string()))
                .collect(),
        );
        for (p, f) in self.creates.clone() {
            if line.starts_with(p.as_str()) {
                self.files.insert(f, (String::new(), 0o644));
            }
        }
        // The latest matching answer wins; a one-shot answer is then spent.
        let Some(i) = self
            .answers
            .iter()
            .rposition(|(p, _, _)| line.starts_with(p.as_str()))
        else {
            return Ok(Output::default());
        };
        let out = self.answers[i].1.clone();
        if self.answers[i].2 {
            self.answers.remove(i);
        }
        Ok(out)
    }

    fn list(&self, dir: &str) -> Vec<String> {
        self.dirs.get(dir).cloned().unwrap_or_default()
    }

    fn live(&self) -> bool {
        true
    }
}
