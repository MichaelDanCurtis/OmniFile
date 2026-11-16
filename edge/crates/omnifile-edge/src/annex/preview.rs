//! The dry-run preview (`annex.md` §4.5): a rule set evaluated over the
//! annex's latest listing, with the same evaluator the annex runs for real
//! (`rules.rs`). It reads metadata, plus the captured ACL's owner and group
//! where a rule's conditions ask for them (cached in `annex.db`'s `facts`).
//! Nothing is written to the NAS, and **no file is read**: a content type is
//! known only once the copier has read the file (its hash pass sniffs the
//! first 4 KB), so a content-type condition on a file not yet copied is
//! unknown, and the file counts as undecided. A real arrival does sniff the
//! file, which moves its atime (the editor says so).
//!
//! The report: per rule, the files and bytes it decides now, with up to 20
//! sample paths; what would be copied, excluded or left undecided; and when
//! files would leave the NAS: now, then per day for a year, plus the files
//! whose removal can't be decided now.
//!
//! It runs off the async loop on its own `annex.db` connection, within a
//! time budget; past it, the run fails rather than holding the edge.

use std::collections::BTreeMap;
use std::time::Instant;

use omnifile_identity::IdMap;
use serde_json::{Value, json};

use super::acl::{self, Flavour};
use super::db::{AnnexDb, Entry};
use super::nasfs::Root;
use super::rules::{DAY_MS, Decision, Facts, Lookup, Removal, RuleSet, Trigger, sniff};
use super::scan::rfc3339_ms;

pub const SAMPLES: usize = 20;
pub const HORIZON_DAYS: i64 = 365;

/// The lookups that cost a read, cached in `facts`.
pub struct Look<'a> {
    pub db: &'a AnnexDb,
    pub fs: Option<&'a Root>,
    pub flavour: Flavour,
    pub ids: &'a dyn IdMap,
    pub path: String,
    pub mtime_ns: i64,
    /// Read the first 4 KB when the content type isn't cached (a real
    /// arrival); a preview never does (it would move the file's atime).
    pub sniff: bool,
}

impl Lookup for Look<'_> {
    fn owner_group(&mut self) -> Option<(Option<String>, Option<String>)> {
        let f = self.db.facts(&self.path).ok()?;
        if f.owner_sid.is_some() || f.group_sid.is_some() {
            return Some((f.owner_sid, f.group_sid));
        }
        let fs = self.fs?;
        let st = fs.stat(&self.path, false).ok()?;
        // An ACL that can't be read is unknown: nothing is decided by it.
        let a = acl::capture(fs, &self.path, self.flavour, &st, self.ids).ok()?;
        let (o, g) = acl::owner_group(&a);
        let _ = self
            .db
            .set_owner_group(&self.path, o.as_deref(), g.as_deref());
        Some((o, g))
    }

    fn content_type(&mut self) -> Option<Option<String>> {
        let f = self.db.facts(&self.path).ok()?;
        if f.ct_mtime == Some(self.mtime_ns) {
            return Some(f.content_type);
        }
        if !self.sniff {
            return None;
        }
        let fs = self.fs?;
        let mut head = vec![0u8; 4096];
        let n = {
            use std::io::Read;
            let mut file = fs.open_file(&self.path).ok()?;
            let mut got = 0;
            while got < head.len() {
                match file.read(&mut head[got..]) {
                    Ok(0) => break,
                    Ok(k) => got += k,
                    Err(_) => return None,
                }
            }
            got
        };
        let t = sniff(&head[..n]).map(str::to_owned);
        let _ = self
            .db
            .set_content_type(&self.path, t.as_deref(), self.mtime_ns);
        Some(t)
    }
}

/// The evaluator's facts for one listed file.
pub fn facts_of(db: &AnnexDb, e: &Entry) -> Facts {
    let f = db.facts(&e.path).unwrap_or_default();
    // A placed file's age counts from its placement (section 4.6): its old
    // dates came back with it.
    let placed = super::placer::placed_at(db, &e.path).unwrap_or(0);
    let placed_ns = placed.saturating_mul(1_000_000);
    Facts {
        path: e.path.clone(),
        size: e.seen.size,
        first_seen_ms: e.first_seen.max(placed),
        mtime_ns: e.seen.mtime_ns.max(placed_ns),
        atime_ns: f.atime_ns.map(|a| a.max(placed_ns)),
    }
}

#[derive(Default)]
struct Tally {
    files: u64,
    bytes: u64,
    samples: Vec<String>,
}

impl Tally {
    fn add(&mut self, path: &str, size: u64) {
        self.files += 1;
        self.bytes += size;
        if self.samples.len() < SAMPLES {
            self.samples.push(path.to_owned());
        }
    }

    fn json(&self) -> Value {
        json!({"files": self.files, "bytes": self.bytes})
    }
}

/// Evaluate `rules` over every file in the listing, giving up at `deadline`.
pub fn dry_run(
    db: &AnnexDb,
    rules: &RuleSet,
    fs: Option<&Root>,
    flavour: Flavour,
    ids: &dyn IdMap,
    now_ms: i64,
    deadline: Option<Instant>,
) -> Result<Value, String> {
    let horizon = HORIZON_DAYS * DAY_MS as i64;
    let mut per_rule: BTreeMap<String, Tally> = BTreeMap::new();
    let (mut all, mut copied, mut excluded, mut undecided) = (
        Tally::default(),
        Tally::default(),
        Tally::default(),
        Tally::default(),
    );
    let mut now_out = Tally::default();
    let mut removal_undecided = Tally::default();
    let mut by_day: BTreeMap<String, Tally> = BTreeMap::new();
    let mut after = String::new();
    loop {
        let page = db
            .entries_after(&after, 5000)
            .map_err(|e| format!("annex.db: {e}"))?;
        let Some(last) = page.last() else { break };
        after = last.path.clone();
        for e in page {
            if deadline.is_some_and(|d| all.files % 256 == 0 && Instant::now() >= d) {
                return Err(format!(
                    "the preview ran out of time after {} files; narrow the rules' \
                     conditions or ask again later",
                    all.files
                ));
            }
            let f = facts_of(db, &e);
            let mut look = Look {
                db,
                fs,
                flavour,
                ids,
                path: e.path.clone(),
                mtime_ns: e.seen.mtime_ns,
                sniff: false,
            };
            all.add(&e.path, f.size);
            match rules.decide(Trigger::Arrived, &f, &mut look, now_ms) {
                Decision::Rule(r) => {
                    per_rule
                        .entry(r.id.clone())
                        .or_default()
                        .add(&e.path, f.size);
                    if r.actions.copy {
                        copied.add(&e.path, f.size);
                    } else {
                        excluded.add(&e.path, f.size);
                    }
                }
                Decision::Undecided(_) | Decision::NoRule => undecided.add(&e.path, f.size),
            }
            if let Decision::Rule(r) = rules.decide(Trigger::Age, &f, &mut look, now_ms) {
                per_rule
                    .entry(r.id.clone())
                    .or_default()
                    .add(&e.path, f.size);
            }
            // The one decision the real run makes (`RuleSet::removal`).
            match rules.removal(&f, &mut look, now_ms, horizon) {
                Removal::At(t, _) if t <= now_ms => now_out.add(&e.path, f.size),
                Removal::At(t, _) => {
                    let day = rfc3339_ms(t)[..10].to_owned();
                    by_day.entry(day).or_default().add(&e.path, f.size);
                }
                Removal::Undecided => removal_undecided.add(&e.path, f.size),
                Removal::Never => {}
            }
        }
    }
    let rules_json: Vec<Value> = rules
        .rules
        .iter()
        .map(|r| {
            let t = per_rule.get(&r.id);
            json!({
                "id": r.id,
                "trigger": r.trigger.name(),
                "files": t.map_or(0, |t| t.files),
                "bytes": t.map_or(0, |t| t.bytes),
                "samples": t.map_or(Vec::new(), |t| t.samples.clone()),
                "copies": r.actions.copy,
                "removes": r.actions.removes(),
            })
        })
        .collect();
    let leaving: u64 = now_out.files + by_day.values().map(|t| t.files).sum::<u64>();
    let leaving_bytes: u64 = now_out.bytes + by_day.values().map(|t| t.bytes).sum::<u64>();
    Ok(json!({
        "evaluated_at": rfc3339_ms(now_ms),
        "rules_version": rules.version,
        "files": all.files,
        "bytes": all.bytes,
        "rules": rules_json,
        "copied": copied.json(),
        "excluded": excluded.json(),
        "undecided": undecided.json(),
        "removals": {
            "within_days": HORIZON_DAYS,
            "files": leaving,
            "bytes": leaving_bytes,
            "now": now_out.json(),
            "undecided": removal_undecided.json(),
            "by_day": by_day.iter().map(|(d, t)| json!({"date": d, "files": t.files, "bytes": t.bytes}))
                .collect::<Vec<_>>(),
        },
    }))
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::annex::rules::parse;
    use crate::annex::scan::Scanner;

    #[test]
    fn the_preview_counts_what_each_rule_decides_and_when_files_leave() {
        let t = tempfile::tempdir().unwrap();
        let w = |p: &str, n: usize| {
            let p = t.path().join(p);
            std::fs::create_dir_all(p.parent().unwrap()).unwrap();
            std::fs::write(p, vec![1u8; n]).unwrap();
        };
        w("surveys/raw/a.segy", 100);
        w("surveys/raw/b.segy", 200);
        w("surveys/scratch/tmp.bin", 50);
        w("notes.txt", 10);
        let db = AnnexDb::open_in_memory().unwrap();
        let now = crate::annex::now_ms() + 60_000;
        Scanner::new(&db, t.path(), now, true).full().unwrap();
        let rules = parse(
            7,
            &json!({"rules": [
                {"id": "scratch", "trigger": "arrived", "conditions": {"path": "**/scratch/*"},
                 "actions": [{"exclude": true}]},
                {"id": "segy", "trigger": "arrived", "conditions": {"extension": ["segy"]},
                 "actions": [{"copy_to": {}}, {"remove_after_days": 30}]},
                {"id": "rest", "trigger": "arrived", "conditions": {}, "actions": [{"copy_to": {}}]},
            ]}),
        )
        .unwrap();
        let fs = Root::open(t.path()).unwrap();
        let ids = omnifile_identity::fake::FakeIdMap::default();
        let r = dry_run(&db, &rules, Some(&fs), Flavour::Nfs3, &ids, now, None).unwrap();
        assert_eq!(r["files"], 4);
        let by_id = |id: &str| {
            r["rules"]
                .as_array()
                .unwrap()
                .iter()
                .find(|x| x["id"] == id)
                .unwrap()
                .clone()
        };
        assert_eq!(by_id("scratch")["files"], 1);
        assert_eq!(by_id("segy")["files"], 2);
        assert_eq!(by_id("segy")["bytes"], 300);
        assert_eq!(by_id("rest")["samples"], json!(["notes.txt"]));
        assert_eq!(r["copied"]["files"], 3);
        assert_eq!(r["excluded"]["files"], 1);
        // The two SEG-Y files leave 30 days after they were first seen.
        assert_eq!(r["removals"]["files"], 2);
        assert_eq!(r["removals"]["now"]["files"], 0);
        let day = r["removals"]["by_day"][0]["date"]
            .as_str()
            .unwrap()
            .to_owned();
        assert_eq!(day, rfc3339_ms(now + 30 * DAY_MS as i64)[..10]);
    }

    #[test]
    fn a_preview_never_reads_a_file_to_tell_its_type() {
        // Reading the first 4 KB moves the file's atime on the NAS: a
        // content-type condition on a file no copy has read is unknown.
        let t = tempfile::tempdir().unwrap();
        std::fs::write(t.path().join("a.bin"), b"PK\x03\x04 a zip").unwrap();
        let db = AnnexDb::open_in_memory().unwrap();
        let now = crate::annex::now_ms() + 60_000;
        Scanner::new(&db, t.path(), now, true).full().unwrap();
        let rules = parse(
            1,
            &json!({"rules": [
                {"id": "zips", "trigger": "arrived", "conditions": {"content_type": ["zip"]},
                 "actions": [{"exclude": true}]},
                {"id": "rest", "trigger": "arrived", "conditions": {}, "actions": [{"copy_to": {}}]},
            ]}),
        )
        .unwrap();
        let fs = Root::open(t.path()).unwrap();
        let ids = omnifile_identity::fake::FakeIdMap::default();
        let r = dry_run(&db, &rules, Some(&fs), Flavour::Nfs3, &ids, now, None).unwrap();
        assert_eq!(r["undecided"]["files"], 1);
        assert_eq!(r["copied"]["files"], 0);
        assert_eq!(
            db.facts("a.bin").unwrap().ct_mtime,
            None,
            "nothing was sniffed (a sniff caches its type)"
        );
        // Once the copier has cached the type, the preview uses it.
        let e = db.entries_after("", 10).unwrap().remove(0);
        db.set_content_type("a.bin", Some("zip"), e.seen.mtime_ns)
            .unwrap();
        let r = dry_run(&db, &rules, Some(&fs), Flavour::Nfs3, &ids, now, None).unwrap();
        assert_eq!(r["excluded"]["files"], 1);
    }
}
