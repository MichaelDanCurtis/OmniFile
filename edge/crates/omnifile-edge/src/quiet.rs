//! Each share's quiet period (decision 67, `docs/design/edge-metadata.md`).
//!
//! A file counts as finished once nobody has written it for its share's
//! period. The edge can't tell which protocol wrote a file (Ganesha and smbd
//! both write the same xfs tree), so the period applies to every write on the
//! share. A share with no period of its own (`quiet_s` in its `share.upsert`
//! row is null or absent) takes its default: `[commit] worm_quiet_s` on a WORM
//! share, `[commit] quiet_s` on any other.
//!
//! The table is rebuilt from the replica every few seconds, so a change
//! reaches a running daemon without a restart.
//!
//! **Never shorter by mistake** (#80 review). A file is due when its period is
//! over, so an error that shortened periods would commit files mid-write, and on
//! a WORM share keep that partial version until retention ends. So:
//! - a read of the replica that fails keeps the table as it was, and doesn't
//!   move the generation ([`QuietPeriods::refresh`]);
//! - before the first good read, and for a path no share resolves, the period is
//!   [`MAX_SHARE_QUIET_S`], the longest any share can have: a share set to 300 s
//!   mustn't get less while its row can't be read;
//! - a read that keeps failing is reported in the heartbeat
//!   (`quiet_stale_s`), so the Edges page and an alert show it.

use std::path::{Path, PathBuf};
use std::sync::RwLock;
use std::sync::atomic::{AtomicU64, Ordering};
use std::time::Duration;

use serde_json::Value;

use crate::config::CommitConfig;

/// The shortest period a share can have. The service refuses less. It is
/// today's default: below it a pause of 20 s commits a partial version, which
/// an ordinary NFS writeback stall can reach, so partial versions would be
/// routine.
pub const MIN_SHARE_QUIET_S: u64 = 10;
/// The longest. The service refuses more. The commit's further wait gives up
/// after about 18 minutes of looking ([`crate::carry`]), and the detector
/// holds an open file per file it waits on, so a much longer period would
/// strand files and use up descriptors.
pub const MAX_SHARE_QUIET_S: u64 = 600;

/// The period a `share.upsert` row asks for. A value the service would have
/// refused (an older or broken row) is brought into the range, never used.
pub fn period_of_row(row: &Value, commit: &CommitConfig) -> Duration {
    let own = row.get("quiet_s").and_then(Value::as_u64);
    let worm = row
        .pointer("/worm/enabled")
        .and_then(Value::as_bool)
        .unwrap_or(false);
    Duration::from_secs(match own {
        Some(s) => s.clamp(MIN_SHARE_QUIET_S, MAX_SHARE_QUIET_S),
        None if worm => commit.worm_quiet_s,
        None => commit.quiet_s,
    })
}

/// Each share's root below the data root, with its period, from the replica's
/// `share.upsert` rows. `dir_path` gives a root folder's path; a share whose
/// root isn't on disk yet (`Ok(None)`) has no files, and is left out. A row that
/// can't be read, or a root whose path can't be read, is an error: the table
/// would be incomplete, and a file of that share would take another's period.
pub fn roots_of<E: std::fmt::Display>(
    rows: &[(u64, u64, String)],
    commit: &CommitConfig,
    dir_path: impl Fn(u64) -> Result<Option<PathBuf>, E>,
) -> Result<Vec<(PathBuf, Duration)>, String> {
    let mut out = Vec::with_capacity(rows.len());
    for (share_id, _, body) in rows {
        let row = serde_json::from_str::<Value>(body)
            .map_err(|e| format!("share {share_id}: its row doesn't parse: {e}"))?;
        let root = row
            .get("root_dir_id")
            .and_then(Value::as_str)
            .and_then(|r| r.parse::<u64>().ok())
            .ok_or_else(|| format!("share {share_id}: no root_dir_id"))?;
        match dir_path(root) {
            Ok(Some(rel)) => out.push((rel, period_of_row(&row, commit))),
            Ok(None) => {}
            Err(e) => return Err(format!("share {share_id}: its root's path: {e}")),
        }
    }
    Ok(out)
}

/// Every share's period, by where its root is.
#[derive(Debug)]
pub struct QuietPeriods {
    data_root: PathBuf,
    /// For a path no share resolves, and for every path until the table is
    /// first read: the longest period a share can have.
    unresolved: Duration,
    /// Since when the table can't be read (the first failure in a row).
    failing_since: std::sync::Mutex<Option<std::time::Instant>>,
    /// Deepest root first, so a file under a shared folder takes that folder's
    /// share, as `Replica::share_of` does.
    roots: RwLock<Vec<(PathBuf, Duration)>>,
    generation: AtomicU64,
}

impl QuietPeriods {
    pub fn new(data_root: &Path, commit: &CommitConfig) -> Self {
        QuietPeriods {
            data_root: data_root.to_path_buf(),
            unresolved: Duration::from_secs(
                MAX_SHARE_QUIET_S
                    .max(commit.quiet_s)
                    .max(commit.worm_quiet_s),
            ),
            failing_since: std::sync::Mutex::new(None),
            roots: RwLock::new(Vec::new()),
            generation: AtomicU64::new(0),
        }
    }

    /// The period for a path below the data root.
    pub fn of_rel(&self, rel: &Path) -> Duration {
        self.roots
            .read()
            .unwrap()
            .iter()
            .find(|(root, _)| rel.starts_with(root))
            .map_or(self.unresolved, |(_, q)| *q)
    }

    /// Rebuild the table from a read of the replica. A failed read, or a share
    /// whose root can't be read, leaves the table and its generation as they
    /// were: files keep the periods they have. Returns whether it was rebuilt.
    pub fn refresh<E: std::fmt::Display, F: std::fmt::Display>(
        &self,
        rows: Result<Vec<(u64, u64, String)>, E>,
        commit: &CommitConfig,
        dir_path: impl Fn(u64) -> Result<Option<PathBuf>, F>,
    ) -> bool {
        let roots = rows
            .map_err(|e| format!("reading the shares failed: {e}"))
            .and_then(|rows| roots_of(&rows, commit, dir_path));
        match roots {
            Ok(roots) => {
                self.set(roots);
                *self.failing_since.lock().unwrap() = None;
                true
            }
            Err(e) => {
                let now = std::time::Instant::now();
                let since = *self.failing_since.lock().unwrap().get_or_insert(now);
                tracing::warn!(err = %e, failing_s = now.duration_since(since).as_secs(),
                               "the shares' quiet periods can't be read: periods kept");
                false
            }
        }
    }

    /// How long the table has failed to be read, every read since the first
    /// failure included; zero while reads work. Sent in the heartbeat.
    pub fn stale_for(&self) -> Duration {
        self.failing_since
            .lock()
            .unwrap()
            .map_or(Duration::ZERO, |t| t.elapsed())
    }

    /// Replace the table. The generation moves only if something changed, so
    /// the detector looks again at the files it waits on only then.
    pub fn set(&self, mut roots: Vec<(PathBuf, Duration)>) {
        roots.sort_by(|a, b| {
            let depth = |p: &Path| p.components().count();
            depth(&b.0).cmp(&depth(&a.0)).then_with(|| a.0.cmp(&b.0))
        });
        let mut cur = self.roots.write().unwrap();
        if *cur != roots {
            *cur = roots;
            self.generation.fetch_add(1, Ordering::SeqCst);
        }
    }
}

impl omnifile_detect::QuietSource for QuietPeriods {
    fn quiet_of(&self, path: &Path) -> Duration {
        path.strip_prefix(&self.data_root)
            .map_or(self.unresolved, |rel| self.of_rel(rel))
    }

    fn generation(&self) -> u64 {
        self.generation.load(Ordering::SeqCst)
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use omnifile_detect::QuietSource;
    use serde_json::json;

    fn commit() -> CommitConfig {
        CommitConfig::default()
    }

    fn secs(s: u64) -> Duration {
        Duration::from_secs(s)
    }

    /// Decision 67: 60 s on a WORM share, today's 10 s elsewhere, and an
    /// existing share (no `quiet_s` in its row) takes its default.
    #[test]
    fn a_share_with_no_period_takes_its_default() {
        let c = commit();
        assert_eq!(c.quiet_s, 10, "today's period");
        assert_eq!(c.worm_quiet_s, 60);
        let worm = json!({"worm": {"enabled": true}});
        let plain = json!({"worm": {"enabled": false}});
        assert_eq!(period_of_row(&worm, &c), secs(60));
        assert_eq!(period_of_row(&plain, &c), secs(10));
        // An older service's row, and an explicit null.
        assert_eq!(period_of_row(&json!({}), &c), secs(10));
        let null = json!({"quiet_s": null, "worm": {"enabled": true}});
        assert_eq!(period_of_row(&null, &c), secs(60));
    }

    #[test]
    fn a_share_s_own_period_wins_over_its_default() {
        let c = commit();
        let row = json!({"quiet_s": 45, "worm": {"enabled": true}});
        assert_eq!(period_of_row(&row, &c), secs(45));
        let row = json!({"quiet_s": 300, "worm": {"enabled": false}});
        assert_eq!(period_of_row(&row, &c), secs(300));
    }

    /// The dev stack's defaults come from its config, not from constants.
    #[test]
    fn the_defaults_come_from_the_edge_config() {
        let mut c = commit();
        c.quiet_s = 3;
        c.worm_quiet_s = 4;
        assert_eq!(period_of_row(&json!({}), &c), secs(3));
        assert_eq!(
            period_of_row(&json!({"worm": {"enabled": true}}), &c),
            secs(4)
        );
    }

    /// A row the service would have refused doesn't make partial versions
    /// routine, or strand files.
    #[test]
    fn a_period_out_of_range_is_brought_into_it() {
        let c = commit();
        assert_eq!(period_of_row(&json!({"quiet_s": 0}), &c), secs(10));
        assert_eq!(period_of_row(&json!({"quiet_s": 86400}), &c), secs(600));
    }

    fn body(share: u64, root: u64, quiet: Value, worm: bool) -> (u64, u64, String) {
        let row = json!({"share_id": share.to_string(), "root_dir_id": root.to_string(),
                         "quiet_s": quiet, "worm": {"enabled": worm}});
        (share, 1, row.to_string())
    }

    /// A file takes the nearest share above it, a folder share over the root
    /// share it sits in, and only whole path components match.
    #[test]
    fn a_file_takes_the_period_of_the_nearest_share() {
        let rows = vec![
            body(1, 100, json!(null), false),
            body(2, 101, json!(120), false),
            body(3, 102, json!(null), true),
            // Not on disk yet: left out.
            body(4, 999, json!(30), false),
        ];
        let q = QuietPeriods::new(Path::new("/d"), &commit());
        let roots = roots_of(&rows, &commit(), |id| {
            Ok::<_, String>(match id {
                100 => Some("seismic".into()),
                101 => Some("seismic/slow".into()),
                102 => Some("legal".into()),
                _ => None,
            })
        })
        .unwrap();
        assert_eq!(roots.len(), 3);
        q.set(roots);
        assert_eq!(q.of_rel(Path::new("seismic/a.segy")), secs(10));
        assert_eq!(q.of_rel(Path::new("seismic/slow/b/c.segy")), secs(120));
        assert_eq!(q.of_rel(Path::new("seismic/slower/c.segy")), secs(10));
        assert_eq!(q.of_rel(Path::new("legal/d.pdf")), secs(60));
        // By absolute path, as the detector asks.
        assert_eq!(q.quiet_of(Path::new("/d/legal/d.pdf")), secs(60));
    }

    /// #80 review: before the table is first read, every path takes the
    /// longest period a share can have, so a share set to 300 s never gets less.
    #[test]
    fn before_the_first_read_every_path_takes_the_longest_period() {
        let q = QuietPeriods::new(Path::new("/d"), &commit());
        assert_eq!(q.of_rel(Path::new("legal/d.pdf")), secs(MAX_SHARE_QUIET_S));
        assert_eq!(
            q.quiet_of(Path::new("/d/seismic/a.segy")),
            secs(MAX_SHARE_QUIET_S)
        );
    }

    /// #80 review: a path no share resolves (a root renamed away, or outside
    /// the data root) takes the longest period.
    #[test]
    fn a_path_no_share_resolves_takes_the_longest_period() {
        let q = QuietPeriods::new(Path::new("/d"), &commit());
        q.set(vec![("seismic".into(), secs(10))]);
        assert_eq!(q.of_rel(Path::new("seismic/a.segy")), secs(10));
        assert_eq!(q.of_rel(Path::new("legal2/d.pdf")), secs(MAX_SHARE_QUIET_S));
        assert_eq!(
            q.quiet_of(Path::new("/elsewhere/legal/d.pdf")),
            secs(MAX_SHARE_QUIET_S)
        );
    }

    /// #80 re-check: a read that keeps failing is counted from its first
    /// failure, for the heartbeat, and a good read clears it.
    #[test]
    fn a_read_that_keeps_failing_is_counted_until_one_works() {
        let q = QuietPeriods::new(Path::new("/d"), &commit());
        let none = |_| Ok::<Option<PathBuf>, String>(None);
        assert_eq!(q.stale_for(), Duration::ZERO);
        assert!(!q.refresh(Err::<Vec<_>, _>("busy"), &commit(), none));
        std::thread::sleep(Duration::from_millis(30));
        assert!(!q.refresh(Err::<Vec<_>, _>("busy"), &commit(), none));
        let stale = q.stale_for();
        assert!(
            stale >= Duration::from_millis(30),
            "{stale:?}: from the first failure"
        );
        assert!(q.refresh(Ok::<_, String>(vec![]), &commit(), none));
        assert_eq!(q.stale_for(), Duration::ZERO);
    }

    /// #80 review, the must-fix: a failed read of the replica, a row that
    /// doesn't parse, or a root whose path can't be read keeps the table and
    /// its generation, so no WORM file drops to the shorter period.
    #[test]
    fn a_failed_read_keeps_the_table_and_its_generation() {
        let rows = vec![
            body(1, 100, json!(null), false),
            body(2, 200, json!(null), true),
        ];
        let paths = |id| {
            Ok::<_, String>(match id {
                100 => Some(PathBuf::from("seismic")),
                200 => Some(PathBuf::from("legal")),
                _ => None,
            })
        };
        let q = QuietPeriods::new(Path::new("/d"), &commit());
        assert!(q.refresh(Ok::<_, String>(rows.clone()), &commit(), paths));
        let g = q.generation();
        assert_eq!(q.of_rel(Path::new("legal/d.pdf")), secs(60));
        // The replica is busy.
        assert!(!q.refresh(Err::<Vec<_>, _>("database is locked"), &commit(), paths));
        assert_eq!(
            (q.generation(), q.of_rel(Path::new("legal/d.pdf"))),
            (g, secs(60))
        );
        // One root's path can't be read.
        let one_fails = |id| match id {
            200 => Err("database is locked".to_string()),
            _ => paths(id),
        };
        assert!(!q.refresh(Ok::<_, String>(rows.clone()), &commit(), one_fails));
        assert_eq!(
            (q.generation(), q.of_rel(Path::new("legal/d.pdf"))),
            (g, secs(60))
        );
        // A row that doesn't parse, or has no root.
        let mut bad = rows.clone();
        bad.push((3, 1, "{not json".into()));
        assert!(!q.refresh(Ok::<_, String>(bad), &commit(), paths));
        let mut bad = rows.clone();
        bad.push((4, 1, json!({"share_id": "4"}).to_string()));
        assert!(!q.refresh(Ok::<_, String>(bad), &commit(), paths));
        assert_eq!(
            (q.generation(), q.of_rel(Path::new("legal/d.pdf"))),
            (g, secs(60))
        );
        // A root not on disk yet is no error: that share has no files.
        let mut later = rows.clone();
        later.push(body(5, 999, json!(30), false));
        assert!(q.refresh(Ok::<_, String>(later), &commit(), paths));
        assert_eq!(q.generation(), g, "nothing changed");
    }

    /// Before the first good read, every path stays at the longest period.
    #[test]
    fn a_failed_first_read_leaves_every_path_at_the_longest_period() {
        let q = QuietPeriods::new(Path::new("/d"), &commit());
        let g = q.generation();
        let none = |_| Ok::<Option<PathBuf>, String>(None);
        assert!(!q.refresh(Err::<Vec<_>, _>("database is locked"), &commit(), none));
        assert_eq!(q.generation(), g);
        assert_eq!(
            q.of_rel(Path::new("seismic/a.segy")),
            secs(MAX_SHARE_QUIET_S)
        );
    }

    /// A change is seen at once by anyone asking, and moves the generation
    /// only when something changed, so the detector re-reads only then.
    #[test]
    fn a_change_takes_effect_and_moves_the_generation() {
        let q = QuietPeriods::new(Path::new("/d"), &commit());
        let g0 = q.generation();
        q.set(vec![("a".into(), secs(10))]);
        let g1 = q.generation();
        assert_ne!(g0, g1);
        q.set(vec![("a".into(), secs(10))]);
        assert_eq!(q.generation(), g1, "no change");
        q.set(vec![("a".into(), secs(60))]);
        assert_ne!(q.generation(), g1);
        assert_eq!(q.of_rel(Path::new("a/f")), secs(60));
    }
}
