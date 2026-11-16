//! The annex's rules (`annex.md` §4.1–4.2, §4.5): one evaluator, used both
//! for the dry-run preview and for real, so the preview can't drift from
//! what happens.
//!
//! A rule set's `body` is `{rules: [{id?, trigger, conditions, actions}]}`.
//! The service validates it before anyone can approve it
//! (`service/…/annex/rules.py`); the edge parses it again and **fails
//! closed**: a body it can't read stops the annex's copying with a problem,
//! and is never guessed at.
//!
//! - **Triggers:** `arrived` (a file passed its quiet period, first time),
//!   `changed` (a copied file's content changed), `age` (checked daily) and
//!   `deleted_at_source` (`keep_copy` only). The return path's triggers
//!   (`recall_requested`, `recall_completed`, phase A3) are the service's to
//!   evaluate, never the edge's: their rules are skipped here (each id still
//!   counts, so ids stay unique), and a placement reaches the edge as a
//!   ticket naming everything it needs. Any other trigger is refused.
//! - **Conditions** (all must match; none means "any"): `path` (globs, `*`,
//!   `?` and `**`), `extension`, `size_gte` / `size_lt` (bytes),
//!   `age_since_arrival_days_gt`, `modified_age_days_gt`,
//!   `accessed_age_days_gt`, `owner_sids`, `group_sids` and `content_type`
//!   (`segy`, `zip`, `hdf5`, sniffed from the first bytes).
//! - **Actions:** `exclude`, `copy_to`, `remove_from_source`,
//!   `remove_after_days`, `policy`, `tag`; `keep_copy` for
//!   `deleted_at_source`.
//! - **Order:** within a trigger, the first matching rule wins, with all its
//!   actions.
//! - **Unknowns:** a condition the edge can't evaluate now (an ACL it can't
//!   read, a content type not sniffed, an atime it doesn't have) is neither
//!   true nor false. A rule whose match is unknown, at or before the rule
//!   that would decide, leaves the file **undecided**: never copied by it,
//!   never removed, counted apart in the preview.
//! - **Limits** (the service checks them too): 200 rules, unique ids, 100
//!   values per list, globs of 512 characters, 32 segments and 4 `**`.
//!
//! **No removal happens in phase A2.1.** A rule that removes produces
//! `would_remove` events only; the removal pipeline is A2.2's.

use serde_json::Value;

/// Milliseconds in a day.
pub const DAY_MS: f64 = 86_400_000.0;

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Trigger {
    Arrived,
    Changed,
    Age,
    DeletedAtSource,
}

impl Trigger {
    fn parse(s: &str) -> Result<Trigger, String> {
        Ok(match s {
            "arrived" => Trigger::Arrived,
            "changed" => Trigger::Changed,
            "age" => Trigger::Age,
            "deleted_at_source" => Trigger::DeletedAtSource,
            other => return Err(format!("trigger {other:?} isn't one this edge runs")),
        })
    }

    pub fn name(self) -> &'static str {
        match self {
            Trigger::Arrived => "arrived",
            Trigger::Changed => "changed",
            Trigger::Age => "age",
            Trigger::DeletedAtSource => "deleted_at_source",
        }
    }
}

#[derive(Clone, Debug, Default, PartialEq)]
pub struct Conditions {
    pub path: Vec<String>,
    pub extension: Vec<String>,
    pub size_gte: Option<u64>,
    pub size_lt: Option<u64>,
    pub age_since_arrival_days_gt: Option<f64>,
    pub modified_age_days_gt: Option<f64>,
    pub accessed_age_days_gt: Option<f64>,
    pub owner_sids: Vec<String>,
    pub group_sids: Vec<String>,
    pub content_type: Vec<String>,
}

impl Conditions {
    pub fn is_empty(&self) -> bool {
        *self == Conditions::default()
    }

    /// Conditions that need the file's ACL or first bytes.
    pub fn needs_owner(&self) -> bool {
        !self.owner_sids.is_empty() || !self.group_sids.is_empty()
    }
}

#[derive(Clone, Debug, Default, PartialEq)]
pub struct Actions {
    pub exclude: bool,
    pub copy: bool,
    pub remove_from_source: bool,
    pub remove_after_days: Option<f64>,
    pub policy: Option<String>,
    pub tags: Vec<(String, String)>,
    pub keep_copy: bool,
}

impl Actions {
    /// The rule takes the file off the NAS, now or later.
    pub fn removes(&self) -> bool {
        self.remove_from_source || self.remove_after_days.is_some()
    }
}

#[derive(Clone, Debug, PartialEq)]
pub struct Rule {
    pub id: String,
    pub trigger: Trigger,
    pub conditions: Conditions,
    pub actions: Actions,
}

#[derive(Clone, Debug, Default, PartialEq)]
pub struct RuleSet {
    pub version: u64,
    pub rules: Vec<Rule>,
}

/// What the evaluator knows of a file. The ACL owner/group and the content
/// type are looked up only when a rule asks (`Lookup`).
#[derive(Clone, Debug, Default)]
pub struct Facts {
    pub path: String,
    pub size: u64,
    /// When the annex first saw it (ms).
    pub first_seen_ms: i64,
    pub mtime_ns: i64,
    pub atime_ns: Option<i64>,
}

/// The facts that cost a read: the ACL's owner and group, the first bytes.
/// `None` is "unknown now" (it couldn't be read, or isn't read in a
/// preview); `Some(None)` is a known absence.
pub trait Lookup {
    fn owner_group(&mut self) -> Option<(Option<String>, Option<String>)>;
    fn content_type(&mut self) -> Option<Option<String>>;
}

/// No lookups: conditions that need one are unknown.
pub struct NoLookup;

impl Lookup for NoLookup {
    fn owner_group(&mut self) -> Option<(Option<String>, Option<String>)> {
        None
    }
    fn content_type(&mut self) -> Option<Option<String>> {
        None
    }
}

/// A three-valued match.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Tri {
    Yes,
    No,
    Unknown,
}

impl Tri {
    fn and(self, other: impl FnOnce() -> Tri) -> Tri {
        match self {
            Tri::No => Tri::No,
            Tri::Yes => other(),
            Tri::Unknown => match other() {
                Tri::No => Tri::No,
                _ => Tri::Unknown,
            },
        }
    }

    fn of(b: bool) -> Tri {
        if b { Tri::Yes } else { Tri::No }
    }
}

/// What a trigger's rules decide for one file.
#[derive(Clone, Copy, Debug)]
pub enum Decision<'a> {
    /// This rule matched first.
    Rule(&'a Rule),
    /// This rule's match is unknown, and no earlier one matched: nobody can
    /// say which rule applies.
    Undecided(&'a Rule),
    /// No rule of the trigger matched.
    NoRule,
}

impl<'a> Decision<'a> {
    pub fn rule(self) -> Option<&'a Rule> {
        match self {
            Decision::Rule(r) => Some(r),
            _ => None,
        }
    }
}

/// When a file would leave the NAS.
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum Removal {
    /// At this time (ms; `now` or later), by this rule.
    At(i64, String),
    /// Not within the horizon, or never (not copied, excluded).
    Never,
    /// A condition at or before the deciding rule is unknown: no removal.
    Undecided,
}

/// Limits the edge enforces itself (as the service does).
pub const MAX_RULES: usize = 200;
pub const MAX_LIST: usize = 100;
pub const MAX_GLOB: usize = 512;
pub const MAX_GLOB_SEGMENTS: usize = 32;
pub const MAX_GLOB_STARSTARS: usize = 4;

fn check_glob(g: &str) -> Result<(), String> {
    if g.is_empty() || g.len() > MAX_GLOB {
        return Err(format!("a path glob is 1 to {MAX_GLOB} characters"));
    }
    let segs: Vec<&str> = g.trim_start_matches('/').split('/').collect();
    if segs.len() > MAX_GLOB_SEGMENTS {
        return Err(format!(
            "a path glob has at most {MAX_GLOB_SEGMENTS} segments"
        ));
    }
    if segs.iter().filter(|s| **s == "**").count() > MAX_GLOB_STARSTARS {
        return Err(format!("a path glob has at most {MAX_GLOB_STARSTARS} `**`"));
    }
    if segs.iter().any(|s| s.is_empty() || *s == "." || *s == "..") {
        return Err("a path glob has no empty, `.` or `..` segments".into());
    }
    Ok(())
}

fn strs(v: &Value, what: &str) -> Result<Vec<String>, String> {
    let out = strs_any(v, what)?;
    if out.is_empty() || out.len() > MAX_LIST {
        return Err(format!("{what} has 1 to {MAX_LIST} values"));
    }
    Ok(out)
}

fn strs_any(v: &Value, what: &str) -> Result<Vec<String>, String> {
    match v {
        Value::String(s) => Ok(vec![s.clone()]),
        Value::Array(a) => a
            .iter()
            .map(|x| {
                x.as_str()
                    .map(str::to_owned)
                    .ok_or_else(|| format!("{what} must be strings"))
            })
            .collect(),
        _ => Err(format!("{what} must be a string or a list of them")),
    }
}

fn days(v: &Value, what: &str) -> Result<f64, String> {
    v.as_f64()
        .filter(|d| d.is_finite() && *d >= 0.0)
        .ok_or_else(|| format!("{what} must be a number of days, 0 or more"))
}

fn bytes(v: &Value, what: &str) -> Result<u64, String> {
    v.as_u64().ok_or_else(|| format!("{what} must be bytes"))
}

fn parse_conditions(v: &Value) -> Result<Conditions, String> {
    let Some(m) = v.as_object() else {
        return Err("conditions must be an object".into());
    };
    let mut c = Conditions::default();
    for (k, v) in m {
        match k.as_str() {
            "path" => {
                c.path = strs(v, "path")?;
                for g in &c.path {
                    check_glob(g)?;
                }
            }
            "extension" => {
                c.extension = strs(v, "extension")?
                    .into_iter()
                    .map(|e| e.trim_start_matches('.').to_ascii_lowercase())
                    .collect()
            }
            "size_gte" => c.size_gte = Some(bytes(v, k)?),
            "size_lt" => c.size_lt = Some(bytes(v, k)?),
            "age_since_arrival_days_gt" => c.age_since_arrival_days_gt = Some(days(v, k)?),
            "modified_age_days_gt" => c.modified_age_days_gt = Some(days(v, k)?),
            "accessed_age_days_gt" => c.accessed_age_days_gt = Some(days(v, k)?),
            "owner_sids" => c.owner_sids = strs(v, k)?,
            "group_sids" => c.group_sids = strs(v, k)?,
            "content_type" => c.content_type = strs(v, k)?,
            other => return Err(format!("condition {other:?} isn't one this edge knows")),
        }
    }
    Ok(c)
}

fn parse_actions(v: &Value) -> Result<Actions, String> {
    let Some(list) = v.as_array() else {
        return Err("actions must be a list".into());
    };
    let mut a = Actions::default();
    for item in list {
        let Some(m) = item.as_object().filter(|m| m.len() == 1) else {
            return Err("each action is an object with one key".into());
        };
        let (k, v) = m.iter().next().unwrap();
        match k.as_str() {
            "exclude" => a.exclude = true,
            "copy_to" => a.copy = true,
            "remove_from_source" => a.remove_from_source = true,
            "remove_after_days" => a.remove_after_days = Some(days(v, k)?),
            "policy" => {
                a.policy = v
                    .get("policy_id")
                    .and_then(Value::as_str)
                    .map(str::to_owned)
            }
            "tag" => {
                let key = v.get("key").and_then(Value::as_str).unwrap_or("");
                let val = v.get("value").and_then(Value::as_str).unwrap_or("");
                if key.is_empty() {
                    return Err("a tag needs a key".into());
                }
                a.tags.push((key.to_owned(), val.to_owned()));
            }
            "keep_copy" => a.keep_copy = true,
            other => return Err(format!("action {other:?} isn't one this edge runs")),
        }
    }
    if a.exclude && (a.copy || a.removes()) {
        return Err("exclude can't be combined with other actions".into());
    }
    Ok(a)
}

/// The return path's triggers: the service evaluates their rules.
pub const RETURN_TRIGGERS: [&str; 2] = ["recall_requested", "recall_completed"];

/// Parse an approved rule set (`{version, body}` from `GET annexes`).
pub fn parse(version: u64, body: &Value) -> Result<RuleSet, String> {
    let Some(rules) = body.get("rules").and_then(Value::as_array) else {
        return Err("the rule set has no rules".into());
    };
    if rules.is_empty() || rules.len() > MAX_RULES {
        return Err(format!("a rule set has 1 to {MAX_RULES} rules"));
    }
    let mut out = Vec::with_capacity(rules.len());
    let mut ids = std::collections::HashSet::new();
    let mut per_trigger = std::collections::HashMap::<&'static str, usize>::new();
    for r in rules {
        let name = r.get("trigger").and_then(Value::as_str).unwrap_or("");
        if RETURN_TRIGGERS.contains(&name) {
            // The service's (annex/returns.py): it decides where a recalled
            // file goes, and the edge places it only on a ticket.
            let id = r.get("id").and_then(Value::as_str).unwrap_or("");
            if id.is_empty() || !ids.insert(id.to_owned()) {
                return Err(format!("return rule id {id:?} is missing or appears twice"));
            }
            continue;
        }
        let trigger = Trigger::parse(name)?;
        let n = per_trigger.entry(trigger.name()).or_default();
        *n += 1;
        let id = r
            .get("id")
            .and_then(Value::as_str)
            .map(str::to_owned)
            .unwrap_or_else(|| format!("{}.{}", trigger.name(), n));
        let empty = Value::Object(Default::default());
        let conditions = parse_conditions(
            r.get("conditions")
                .filter(|v| !v.is_null())
                .unwrap_or(&empty),
        )?;
        if !ids.insert(id.clone()) {
            return Err(format!("rule id {id} appears twice"));
        }
        let actions = parse_actions(r.get("actions").unwrap_or(&Value::Null))?;
        if trigger == Trigger::Age
            && (actions.copy
                || actions.remove_after_days.is_some()
                || actions.policy.is_some()
                || !actions.tags.is_empty()
                || actions.keep_copy)
        {
            return Err(format!("rule {id}: an age rule only excludes or removes"));
        }
        if trigger == Trigger::Changed && actions.removes() {
            // A change is copied again; leaving the NAS is an arrival's or an
            // age rule's call (the preview can only show those).
            return Err(format!("rule {id}: a changed rule doesn't remove"));
        }
        if trigger == Trigger::DeletedAtSource
            && !(actions.keep_copy && !actions.copy && !actions.removes())
        {
            return Err(format!(
                "rule {id}: a deleted_at_source rule keeps the copy"
            ));
        }
        if matches!(trigger, Trigger::Arrived | Trigger::Changed)
            && actions.removes()
            && !actions.copy
        {
            return Err(format!("rule {id}: a rule that removes must copy first"));
        }
        out.push(Rule {
            id,
            trigger,
            conditions,
            actions,
        });
    }
    Ok(RuleSet {
        version,
        rules: out,
    })
}

/// `*` and `?` within one path segment.
fn seg_match(p: &[u8], s: &[u8]) -> bool {
    let (mut pi, mut si) = (0, 0);
    let (mut star, mut mark) = (None, 0);
    while si < s.len() {
        if pi < p.len() && (p[pi] == b'?' || p[pi] == s[si]) {
            pi += 1;
            si += 1;
        } else if pi < p.len() && p[pi] == b'*' {
            star = Some(pi);
            mark = si;
            pi += 1;
        } else if let Some(sp) = star {
            pi = sp + 1;
            mark += 1;
            si = mark;
        } else {
            return false;
        }
    }
    while pi < p.len() && p[pi] == b'*' {
        pi += 1;
    }
    pi == p.len()
}

/// Segment globs against path segments, memoised: O(pattern × path)
/// whatever the `**` (a naive recursion is exponential in them).
fn segs_match(p: &[&str], s: &[&str]) -> bool {
    // ok[i][j]: p[i..] matches s[j..]; filled from the end.
    let (n, m) = (p.len(), s.len());
    let mut ok = vec![vec![false; m + 1]; n + 1];
    ok[n][m] = true;
    for i in (0..n).rev() {
        for j in (0..=m).rev() {
            ok[i][j] = if p[i] == "**" {
                ok[i + 1][j] || (j < m && ok[i][j + 1])
            } else {
                j < m && seg_match(p[i].as_bytes(), s[j].as_bytes()) && ok[i + 1][j + 1]
            };
        }
    }
    ok[0][0]
}

/// A path glob relative to the annex's root: `/`-separated, `*` and `?`
/// within a segment, `**` for any number of segments. A glob without `/`
/// matches the name in any folder.
pub fn glob_match(pattern: &str, path: &str) -> bool {
    let s: Vec<&str> = path.split('/').collect();
    if !pattern.contains('/') {
        return s
            .last()
            .is_some_and(|name| seg_match(pattern.as_bytes(), name.as_bytes()));
    }
    let mut p: Vec<&str> = pattern.trim_start_matches('/').split('/').collect();
    p.dedup_by(|a, b| *a == "**" && *b == "**"); // `**/**` is `**`
    segs_match(&p, &s)
}

fn ext_match(exts: &[String], path: &str) -> bool {
    let name = path.rsplit('/').next().unwrap_or(path).to_ascii_lowercase();
    exts.iter().any(|e| name.ends_with(&format!(".{e}")))
}

fn older(than_days: Option<f64>, since_ms: Option<i64>, now_ms: i64) -> Tri {
    match (than_days, since_ms) {
        (None, _) => Tri::Yes,
        (Some(_), None) => Tri::Unknown,
        (Some(d), Some(t)) => Tri::of((now_ms - t) as f64 > d * DAY_MS),
    }
}

impl Rule {
    /// Whether the rule's conditions hold for the file at `now_ms`.
    pub fn matches(&self, f: &Facts, look: &mut dyn Lookup, now_ms: i64) -> Tri {
        self.static_match(f, look)
            .and(|| self.ages_match(f, now_ms))
    }

    /// Everything but the ages. The cheap, certain conditions first: a
    /// definite no never asks a lookup.
    pub fn static_match(&self, f: &Facts, look: &mut dyn Lookup) -> Tri {
        let c = &self.conditions;
        if !c.path.is_empty() && !c.path.iter().any(|p| glob_match(p, &f.path)) {
            return Tri::No;
        }
        if !c.extension.is_empty() && !ext_match(&c.extension, &f.path) {
            return Tri::No;
        }
        if c.size_gte.is_some_and(|n| f.size < n) || c.size_lt.is_some_and(|n| f.size >= n) {
            return Tri::No;
        }
        let mut owner = || {
            if !c.needs_owner() {
                return Tri::Yes;
            }
            let Some((o, g)) = look.owner_group() else {
                return Tri::Unknown;
            };
            let ok_o = c.owner_sids.is_empty() || o.is_some_and(|o| c.owner_sids.contains(&o));
            let ok_g = c.group_sids.is_empty() || g.is_some_and(|g| c.group_sids.contains(&g));
            Tri::of(ok_o && ok_g)
        };
        let o = owner();
        if o == Tri::No {
            return Tri::No;
        }
        let t = if c.content_type.is_empty() {
            Tri::Yes
        } else {
            match look.content_type() {
                None => Tri::Unknown,
                Some(t) => Tri::of(t.is_some_and(|t| c.content_type.contains(&t))),
            }
        };
        o.and(|| t)
    }

    fn ages_match(&self, f: &Facts, now_ms: i64) -> Tri {
        let c = &self.conditions;
        older(c.age_since_arrival_days_gt, Some(f.first_seen_ms), now_ms)
            .and(|| older(c.modified_age_days_gt, Some(f.mtime_ns / 1_000_000), now_ms))
            .and(|| {
                older(
                    c.accessed_age_days_gt,
                    f.atime_ns.map(|a| a / 1_000_000),
                    now_ms,
                )
            })
    }

    /// The earliest time (ms) the rule's ages hold, `now_ms` or later; `None`
    /// if an age it needs is unknown.
    pub fn ages_from(&self, f: &Facts, now_ms: i64) -> Option<i64> {
        let c = &self.conditions;
        let mut t = now_ms;
        let mut bump = |d: Option<f64>, since: Option<i64>| -> Option<()> {
            if let Some(d) = d {
                let since = since?;
                t = t.max(since + (d * DAY_MS) as i64 + 1);
            }
            Some(())
        };
        bump(c.age_since_arrival_days_gt, Some(f.first_seen_ms))?;
        bump(c.modified_age_days_gt, Some(f.mtime_ns / 1_000_000))?;
        bump(c.accessed_age_days_gt, f.atime_ns.map(|a| a / 1_000_000))?;
        Some(t)
    }
}

impl RuleSet {
    /// What `trigger`'s rules decide: the first that matches, or undecided
    /// if one at or before it can't be evaluated now.
    pub fn decide(
        &self,
        trigger: Trigger,
        f: &Facts,
        look: &mut dyn Lookup,
        now_ms: i64,
    ) -> Decision<'_> {
        for r in self.rules.iter().filter(|r| r.trigger == trigger) {
            match r.matches(f, look, now_ms) {
                Tri::Yes => return Decision::Rule(r),
                Tri::Unknown => return Decision::Undecided(r),
                Tri::No => {}
            }
        }
        Decision::NoRule
    }

    pub fn rule(&self, id: &str) -> Option<&Rule> {
        self.rules.iter().find(|r| r.id == id)
    }

    /// Any rule removes files.
    pub fn removes(&self) -> bool {
        self.rules.iter().any(|r| r.actions.removes())
    }

    /// When a file would first leave the NAS, looking `horizon_ms` ahead.
    /// **The one decision both the preview and the real run use**: the
    /// arrival rules decide under this rule set (never a rule id saved under
    /// another), then `remove_after_days` and the age rules.
    pub fn removal(
        &self,
        f: &Facts,
        look: &mut dyn Lookup,
        now_ms: i64,
        horizon_ms: i64,
    ) -> Removal {
        let arrival = match self.decide(Trigger::Arrived, f, look, now_ms) {
            Decision::Rule(r) => r,
            Decision::Undecided(_) => return Removal::Undecided,
            Decision::NoRule => return Removal::Never,
        };
        if !arrival.actions.copy {
            return Removal::Never; // nothing leaves the NAS without its copy
        }
        if arrival.actions.remove_from_source {
            return Removal::At(now_ms, arrival.id.clone());
        }
        let mut best: Option<(i64, String)> = arrival.actions.remove_after_days.map(|d| {
            (
                (f.first_seen_ms + (d * DAY_MS) as i64).max(now_ms),
                arrival.id.clone(),
            )
        });
        // Age rules: the first that would match on the day it first could.
        let ages: Vec<&Rule> = self
            .rules
            .iter()
            .filter(|r| r.trigger == Trigger::Age)
            .collect();
        for (i, a) in ages.iter().enumerate() {
            match a.static_match(f, look) {
                Tri::No => continue,
                // An unknown at or before a removing rule decides nothing.
                Tri::Unknown => return Removal::Undecided,
                Tri::Yes => {}
            }
            if !a.actions.removes() {
                continue;
            }
            let Some(t) = a.ages_from(f, now_ms) else {
                return Removal::Undecided;
            };
            if t - now_ms > horizon_ms {
                continue;
            }
            // An earlier age rule that matches then wins (an exclude keeps it).
            let mut earlier = Tri::No;
            for e in &ages[..i] {
                match e.matches(f, look, t) {
                    Tri::Yes => {
                        earlier = Tri::Yes;
                        break;
                    }
                    Tri::Unknown => earlier = Tri::Unknown,
                    Tri::No => {}
                }
            }
            match earlier {
                Tri::Yes => continue,
                Tri::Unknown => return Removal::Undecided,
                Tri::No => {}
            }
            if best.as_ref().is_none_or(|(b, _)| t < *b) {
                best = Some((t, a.id.clone()));
            }
        }
        match best {
            Some((t, rule)) if t - now_ms <= horizon_ms => Removal::At(t, rule),
            _ => Removal::Never,
        }
    }
}

/// The content type from a file's first bytes.
pub fn sniff(head: &[u8]) -> Option<&'static str> {
    if head.starts_with(b"PK\x03\x04") {
        return Some("zip");
    }
    if head.starts_with(b"\x89HDF\r\n\x1a\n") {
        return Some("hdf5");
    }
    // SEG-Y: a 3,200-byte textual header (EBCDIC 'C' = 0xC3, or ASCII 'C'),
    // then a 400-byte binary header the SEG-Y check recognises.
    if head.len() >= 3600
        && (head[0] == 0xC3 || head[0] == b'C')
        && super::segy::check(&head[..3600], 3600) != super::segy::Segy::Unrecognised
    {
        return Some("segy");
    }
    None
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    fn facts(path: &str, size: u64, age_days: f64) -> Facts {
        let now = 1_000 * DAY_MS as i64;
        Facts {
            path: path.into(),
            size,
            first_seen_ms: now - (age_days * DAY_MS) as i64,
            mtime_ns: (now - (age_days * DAY_MS) as i64) * 1_000_000,
            atime_ns: None,
        }
    }
    const NOW: i64 = 1_000 * DAY_MS as i64;

    fn archive_after(n: u64) -> RuleSet {
        parse(
            3,
            &json!({"rules": [
                {"id": "scratch", "trigger": "arrived", "conditions": {"path": "surveys/**/scratch/*"},
                 "actions": [{"exclude": true}]},
                {"id": "copy", "trigger": "arrived", "conditions": {}, "actions": [{"copy_to": {}}]},
                {"id": "recopy", "trigger": "changed", "conditions": {}, "actions": [{"copy_to": {}}]},
                {"id": "old", "trigger": "age", "conditions": {"age_since_arrival_days_gt": n},
                 "actions": [{"remove_from_source": true}]},
                {"id": "keep", "trigger": "age", "conditions": {}, "actions": [{"exclude": true}]},
            ]}),
        )
        .unwrap()
    }

    #[test]
    fn globs_match_segments_and_any_depth() {
        assert!(glob_match(
            "surveys/**/raw/*",
            "surveys/2026/north/raw/l.segy"
        ));
        assert!(glob_match("surveys/**/raw/*", "surveys/raw/l.segy"));
        assert!(!glob_match(
            "surveys/**/raw/*",
            "surveys/2026/raw/sub/l.segy"
        ));
        assert!(glob_match("*.segy", "a/b/line_1042.segy"));
        assert!(glob_match("line_10??.segy", "line_1042.segy"));
        assert!(!glob_match("surveys/*", "other/surveys/x"));
    }

    /// Every match decides; an unknown is never a definite one.
    fn id(d: Decision<'_>) -> String {
        match d {
            Decision::Rule(r) => r.id.clone(),
            Decision::Undecided(r) => format!("undecided at {}", r.id),
            Decision::NoRule => "none".into(),
        }
    }

    #[test]
    fn a_glob_with_many_starstars_is_matched_in_polynomial_time() {
        // A naive recursion tries every split of the path among the `**`:
        // with 11 of them over 60 segments that never match, it doesn't end.
        let (tx, rx) = std::sync::mpsc::channel();
        std::thread::spawn(move || {
            let pat = "**/a/**/a/**/a/**/a/**/a/**/a/**/a/**/a/**/a/**/a/**/a/**/b";
            let path = vec!["a"; 60].join("/");
            tx.send(glob_match(pat, &path)).unwrap();
        });
        let got = rx.recv_timeout(std::time::Duration::from_secs(5));
        assert_eq!(got, Ok(false), "the matcher took too long");
        assert!(glob_match("**/**/**/raw/*", "raw/x"), "runs of ** are one");
    }

    #[test]
    fn the_rule_language_limits_are_enforced_by_the_edge_too() {
        let one = |c: Value| {
            json!({"rules": [{"trigger": "arrived", "conditions": c,
                              "actions": [{"copy_to": {}}]}]})
        };
        assert!(parse(1, &one(json!({"path": "**/a/**/b/**/c/**/d/*"}))).is_ok());
        assert!(parse(1, &one(json!({"path": "**/a/**/b/**/c/**/d/**/e"}))).is_err());
        let deep = vec!["d"; 33].join("/");
        assert!(parse(1, &one(json!({"path": deep}))).is_err());
        assert!(parse(1, &one(json!({"path": "a/../b"}))).is_err());
        assert!(parse(1, &one(json!({"extension": vec!["x"; 101]}))).is_err());
        let many: Vec<Value> = (0..201)
            .map(|_| json!({"trigger": "arrived", "actions": [{"copy_to": {}}]}))
            .collect();
        assert!(parse(1, &json!({"rules": many})).is_err());
        let twice = json!({"rules": [
            {"id": "a", "trigger": "arrived", "actions": [{"copy_to": {}}]},
            {"id": "a", "trigger": "arrived", "actions": [{"exclude": true}]}]});
        assert!(parse(1, &twice).unwrap_err().contains("twice"));
    }

    #[test]
    fn return_rules_are_the_services_and_never_decide_anything_here() {
        let body = json!({"rules": [
            {"id": "copy", "trigger": "arrived", "actions": [{"copy_to": {}}]},
            {"id": "back", "trigger": "recall_completed", "conditions": {"requester_in": ["S-1-5-21-1-2-3-4"]},
             "actions": [{"place_at": {"kind": "nas_folder", "origin": true}},
                         {"on_conflict": "restored_name"}, {"hold_days": 14}]}]});
        let rs = parse(3, &body).unwrap();
        assert_eq!(rs.rules.len(), 1, "only the outbound rule is the edge's");
        assert_eq!(
            id(rs.decide(Trigger::Arrived, &facts("a", 1, 1.0), &mut NoLookup, NOW)),
            "copy"
        );
        // Ids stay unique across both, and a return rule needs one.
        let twice = json!({"rules": [
            {"id": "a", "trigger": "arrived", "actions": [{"copy_to": {}}]},
            {"id": "a", "trigger": "recall_completed", "actions": [{"hold_days": 1}]}]});
        assert!(parse(1, &twice).unwrap_err().contains("twice"));
        let unnamed = json!({"rules": [
            {"id": "a", "trigger": "arrived", "actions": [{"copy_to": {}}]},
            {"trigger": "recall_completed", "actions": [{"hold_days": 1}]}]});
        assert!(parse(1, &unnamed).is_err());
        // Anything else unknown is still refused.
        let other =
            json!({"rules": [{"id": "a", "trigger": "schedule", "actions": [{"copy_to": {}}]}]});
        assert!(parse(1, &other).is_err());
    }

    #[test]
    fn changed_rules_never_remove_and_age_rules_only_exclude_or_remove() {
        for bad in [
            json!({"rules": [{"trigger": "changed",
                "actions": [{"copy_to": {}}, {"remove_after_days": 3}]}]}),
            json!({"rules": [{"trigger": "changed",
                "actions": [{"copy_to": {}}, {"remove_from_source": true}]}]}),
            json!({"rules": [{"trigger": "age",
                "actions": [{"remove_from_source": true}, {"tag": {"key": "k", "value": "v"}}]}]}),
            json!({"rules": [{"trigger": "age",
                "actions": [{"remove_from_source": true}, {"policy": {"policy_id": "p"}}]}]}),
            json!({"rules": [{"trigger": "age",
                "actions": [{"remove_from_source": true}, {"keep_copy": true}]}]}),
        ] {
            assert!(parse(1, &bad).is_err(), "{bad}");
        }
    }

    #[test]
    fn the_first_matching_rule_of_the_trigger_wins() {
        let rs = archive_after(30);
        let d = |p: &str| id(rs.decide(Trigger::Arrived, &facts(p, 10, 1.0), &mut NoLookup, NOW));
        assert_eq!(d("surveys/2026/scratch/tmp.bin"), "scratch");
        assert_eq!(d("surveys/2026/raw/line.segy"), "copy");
        let age = |a: f64| id(rs.decide(Trigger::Age, &facts("x", 10, a), &mut NoLookup, NOW));
        assert_eq!(age(31.0), "old");
        assert_eq!(age(29.0), "keep");
    }

    #[test]
    fn removal_days_follow_the_age_rules_and_need_a_copy() {
        let rs = archive_after(30);
        let h = 365 * DAY_MS as i64;
        let f = facts("surveys/raw/l.segy", 10, 10.0);
        let Removal::At(t, rule) = rs.removal(&f, &mut NoLookup, NOW, h) else {
            panic!("no removal");
        };
        assert_eq!(rule, "old");
        let in_days = (t - NOW) as f64 / DAY_MS;
        assert!((20.0..20.01).contains(&in_days), "{in_days}");
        // Excluded on arrival: never removed (no copy).
        let f = facts("surveys/x/scratch/tmp", 10, 10.0);
        assert_eq!(rs.removal(&f, &mut NoLookup, NOW, h), Removal::Never);
    }

    #[test]
    fn an_unknown_at_or_before_the_deciding_rule_decides_nothing() {
        let rs = parse(
            1,
            &json!({"rules": [
                {"id": "zips", "trigger": "arrived", "conditions": {"content_type": ["zip"]},
                 "actions": [{"exclude": true}]},
                {"id": "copy", "trigger": "arrived", "conditions": {}, "actions": [{"copy_to": {}}]},
                {"id": "idle", "trigger": "age", "conditions": {"accessed_age_days_gt": 90},
                 "actions": [{"exclude": true}]},
                {"id": "old", "trigger": "age", "conditions": {"age_since_arrival_days_gt": 30},
                 "actions": [{"remove_from_source": true}]},
            ]}),
        )
        .unwrap();
        let h = 365 * DAY_MS as i64;
        let f = facts("a/l.segy", 10, 40.0);
        // The content type isn't known: neither excluded nor copied.
        assert_eq!(
            id(rs.decide(Trigger::Arrived, &f, &mut NoLookup, NOW)),
            "undecided at zips"
        );
        assert_eq!(rs.removal(&f, &mut NoLookup, NOW, h), Removal::Undecided);
        // Known not a zip, but no atime: the keep-rule ahead of the removal
        // might hold, so no removal either.
        struct NotZip;
        impl Lookup for NotZip {
            fn owner_group(&mut self) -> Option<(Option<String>, Option<String>)> {
                None
            }
            fn content_type(&mut self) -> Option<Option<String>> {
                Some(None)
            }
        }
        assert_eq!(
            id(rs.decide(Trigger::Arrived, &f, &mut NotZip, NOW)),
            "copy"
        );
        assert_eq!(rs.removal(&f, &mut NotZip, NOW, h), Removal::Undecided);
        // With its atime known: read last week, so the keep-rule doesn't
        // hold, and it leaves now.
        let mut seen = f.clone();
        seen.atime_ns = Some((NOW - 7 * DAY_MS as i64) * 1_000_000);
        assert_eq!(
            rs.removal(&seen, &mut NotZip, NOW, h),
            Removal::At(NOW, "old".into())
        );
        // Idle for a year: the keep-rule holds.
        seen.atime_ns = Some((NOW - 365 * DAY_MS as i64) * 1_000_000);
        assert_eq!(rs.removal(&seen, &mut NotZip, NOW, h), Removal::Never);
    }

    #[test]
    fn conditions_on_size_extension_owner_and_content() {
        struct L;
        impl Lookup for L {
            fn owner_group(&mut self) -> Option<(Option<String>, Option<String>)> {
                Some((
                    Some("S-1-5-21-1-2-3-1105".into()),
                    Some("S-1-5-21-1-2-3-2001".into()),
                ))
            }
            fn content_type(&mut self) -> Option<Option<String>> {
                Some(Some("segy".into()))
            }
        }
        let rs = parse(
            1,
            &json!({"rules": [
                {"id": "big", "trigger": "arrived", "conditions": {
                    "extension": ["SEGY", "sgy"], "size_gte": 1000, "group_sids": ["S-1-5-21-1-2-3-2001"],
                    "content_type": ["segy"]},
                 "actions": [{"copy_to": {}}, {"remove_after_days": 30}, {"tag": {"key": "k", "value": "v"}}]},
                {"id": "rest", "trigger": "arrived", "conditions": {}, "actions": [{"exclude": true}]},
            ]}),
        )
        .unwrap();
        let d = |p: &str, size, l: &mut dyn Lookup| {
            id(rs.decide(Trigger::Arrived, &facts(p, size, 1.0), l, NOW))
        };
        assert_eq!(d("a/l.segy", 5000, &mut L), "big");
        assert_eq!(d("a/l.segy", 5, &mut L), "rest");
        assert_eq!(d("a/l.txt", 5000, &mut L), "rest");
        // Without the ACL or first bytes, nothing is decided (never a guess).
        assert_eq!(d("a/l.segy", 5000, &mut NoLookup), "undecided at big");
        // A definite no on a cheap condition never needs the lookup.
        assert_eq!(d("a/l.txt", 5000, &mut NoLookup), "rest");
        assert!(rs.removes());
    }

    #[test]
    fn content_types_are_sniffed() {
        assert_eq!(sniff(b"PK\x03\x04rest"), Some("zip"));
        assert_eq!(sniff(b"\x89HDF\r\n\x1a\nrest"), Some("hdf5"));
        let segy = crate::annex::segy::synthetic(2, 10, 1);
        assert_eq!(sniff(&segy[..3600]), Some("segy"));
        assert_eq!(sniff(b"hello"), None);
    }
}
