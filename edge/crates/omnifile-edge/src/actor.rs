//! Who made a namespace change (`control-api.md` §4.1 `actor`,
//! `docs/design/namespace-sync.md` "Who did it"). Ported from line B (PR
//! #10: d981ee9, 81fa187) onto this line's namespace sync.
//!
//! The namespace sync decides *what* changed by reconciling a directory with
//! the replica; nothing is taken from an event but which directory to look
//! at. *Who* comes from hints, kept here for a few minutes and looked up when
//! a pass queues an op:
//!
//! | Source | Who | Hints |
//! |---|---|---|
//! | `FSAL_OMNI`'s `NS_HINT` (`fsal-socket.md` §5.4) | the NFS caller: uid, `sec`, client address (SID through winbind) | mkdir, unlink, rmdir, rename, chown |
//! | the namespace watcher (`omnifile_detect::ns`, names and child handles) | the smbd session behind the event's pid, or a local process's uid | create, delete, rename, attribute change |
//!
//! An op no hint names is sent with `via = unknown`: a change made while
//! the daemon was down (the sweep at start finds it), one whose event was
//! lost, or one whose process couldn't be resolved. The daemon's own
//! changes never get a hint (its own pid is skipped, and its changes find
//! the replica already agreeing), so they are never put on a person.
//! Ganesha's events are known by the FSAL peer's pid and binary, never by
//! its process name (any process can call itself `ganesha.nfsd`): its NFS
//! callers come from `NS_HINT`, never as a local root process.

use std::collections::HashMap;
use std::time::{Duration, Instant, SystemTime};

use omnifile_control::types as ct;
use omnifile_identity::{Proof, ResolvedIdentity, Sec, Via};

use crate::ports::Actor;

/// An inode: `(ino, generation)`, as the replica and the FSAL key it.
pub type Key = (u64, u32);

/// How long a hint is kept: well past the quiet period and a busy pass.
pub const HINT_TTL: Duration = Duration::from_secs(600);
/// Most hints kept; past it, expired ones go, then the oldest half.
const HINT_MAX: usize = 200_000;

#[derive(Clone, Copy, Debug, PartialEq, Eq, Hash)]
pub enum HintOp {
    /// A new folder.
    Create,
    /// An unlink or rmdir.
    Delete,
    Rename,
    /// An owner or group change.
    Attrib,
}

#[derive(Clone, Debug)]
struct Hint {
    /// One per noted change; a hint indexed both ways shares it, so taking
    /// it once removes it from both.
    seq: u64,
    op: HintOp,
    actor: Actor,
    at: Instant,
    /// When it was noted (after the change happened): a hint older than the
    /// change being attributed is someone else's earlier change.
    wall: SystemTime,
    /// A rename's new name, when the source knows it.
    new_name: Option<String>,
}

/// Recent hints, by the object's inode and by `(directory inode, name)`.
/// A hint is used for one op only ([`Hints::take`]).
#[derive(Default)]
pub struct Hints {
    by_target: HashMap<Key, Vec<Hint>>,
    by_name: HashMap<(Key, String), Vec<Hint>>,
    n: usize,
    seq: u64,
}

/// The clock a change's inode time and a hint's time are compared on can
/// differ by a tick (the kernel's coarse clock lags): this much slack.
const CLOCK_SLACK: Duration = Duration::from_millis(100);

impl Hints {
    /// Record who did `op` to `target` (its inode) or at `at` (its
    /// directory's inode and its name; a rename's new place).
    pub fn note(
        &mut self,
        op: HintOp,
        target: Option<Key>,
        at: Option<(Key, &str)>,
        new_name: Option<&str>,
        actor: Actor,
    ) {
        self.note_at(
            op,
            target,
            at,
            new_name,
            actor,
            Instant::now(),
            SystemTime::now(),
        );
    }

    #[allow(clippy::too_many_arguments)]
    fn note_at(
        &mut self,
        op: HintOp,
        target: Option<Key>,
        at: Option<(Key, &str)>,
        new_name: Option<&str>,
        actor: Actor,
        now: Instant,
        wall: SystemTime,
    ) {
        if target.is_none() && at.is_none() {
            return;
        }
        if self.n >= HINT_MAX {
            self.prune(now);
        }
        self.seq += 1;
        let h = Hint {
            seq: self.seq,
            op,
            actor,
            at: now,
            wall,
            new_name: new_name.map(str::to_owned),
        };
        if let Some(t) = target.filter(|t| t.0 != 0) {
            self.by_target.entry(t).or_default().push(h.clone());
            self.n += 1;
        }
        if let Some((d, name)) = at.filter(|(d, _)| d.0 != 0) {
            self.by_name
                .entry((d, name.to_owned()))
                .or_default()
                .push(h);
            self.n += 1;
        }
    }

    fn prune(&mut self, now: Instant) {
        let fresh = |v: &mut Vec<Hint>| {
            v.retain(|h| now.duration_since(h.at) < HINT_TTL);
            !v.is_empty()
        };
        self.by_target.retain(|_, v| fresh(v));
        self.by_name.retain(|_, v| fresh(v));
        self.recount();
        if self.n >= HINT_MAX {
            // A flood: keep nothing rather than grow (ops go `unknown`).
            self.by_target.clear();
            self.by_name.clear();
            self.n = 0;
        }
    }

    fn recount(&mut self) {
        self.n = self.by_target.values().map(Vec::len).sum::<usize>()
            + self.by_name.values().map(Vec::len).sum::<usize>();
    }

    /// Who did `op` to `target`, or at `at`, for a change made at `changed`
    /// (the object's inode change time, when known): the latest hint that
    /// fits, by the object first, and never one noted before the change (an
    /// earlier change's). A rename's hint that names a new name must name
    /// this one. The hint is used up: a later op that finds no hint of its
    /// own is `unknown`, never this actor's.
    pub fn take(
        &mut self,
        op: HintOp,
        target: Option<Key>,
        at: Option<(Key, &str)>,
        changed: Option<SystemTime>,
    ) -> Option<Actor> {
        let h = self.pick(op, target, at, changed, Instant::now())?;
        let seq = h.seq;
        for v in self.by_target.values_mut().chain(self.by_name.values_mut()) {
            v.retain(|x| x.seq != seq);
        }
        self.by_target.retain(|_, v| !v.is_empty());
        self.by_name.retain(|_, v| !v.is_empty());
        self.recount();
        Some(h.actor)
    }

    /// As [`Hints::take`], leaving the hint for the op it belongs to (a node
    /// moved aside first, whose own pass sends its rename).
    pub fn peek(
        &self,
        op: HintOp,
        target: Option<Key>,
        at: Option<(Key, &str)>,
        changed: Option<SystemTime>,
    ) -> Option<Actor> {
        self.pick(op, target, at, changed, Instant::now())
            .map(|h| h.actor)
    }

    fn pick(
        &self,
        op: HintOp,
        target: Option<Key>,
        at: Option<(Key, &str)>,
        changed: Option<SystemTime>,
        now: Instant,
    ) -> Option<Hint> {
        let name = at.map(|(_, n)| n);
        let not_before = changed.map(|c| c.checked_sub(CLOCK_SLACK).unwrap_or(c));
        let pick = |v: Option<&Vec<Hint>>| -> Option<Hint> {
            v?.iter()
                .rev()
                .find(|h| {
                    h.op == op
                        && now.duration_since(h.at) < HINT_TTL
                        && not_before.is_none_or(|nb| h.wall >= nb)
                        && match (&h.new_name, name) {
                            (Some(hn), Some(n)) if op == HintOp::Rename => hn == n,
                            _ => true,
                        }
                })
                .cloned()
        };
        if let Some(t) = target.filter(|t| t.0 != 0)
            && let Some(h) = pick(self.by_target.get(&t))
        {
            return Some(h);
        }
        let (d, n) = at?;
        pick(self.by_name.get(&(d, n.to_owned())))
    }

    pub fn len(&self) -> usize {
        self.n
    }

    pub fn is_empty(&self) -> bool {
        self.n == 0
    }
}

/// An NFS caller as `FSAL_OMNI` reports it (`fsal-socket.md` §4.2). The SID
/// is looked up when the op is queued. A caller whose uid the FSAL couldn't
/// read is nobody the edge can name.
pub fn nfs_actor(id: &omnifile_fsal_proto::wire::Identity) -> Actor {
    if id.uid == omnifile_fsal_proto::wire::Identity::UNKNOWN_UID {
        return Actor::unknown();
    }
    let sec = match id.sec {
        1 => Some(ct::Sec::Sys),
        2 => Some(ct::Sec::Krb5),
        3 => Some(ct::Sec::Krb5i),
        4 => Some(ct::Sec::Krb5p),
        _ => None,
    };
    Actor {
        via: ct::ActorVia::Nfs,
        uid: Some(id.uid),
        sid: None,
        sec,
        // `sec=sys` (or an unknown flavour) only claims the uid.
        proof: Some(if id.proven() {
            ct::Proof::Proven
        } else {
            ct::Proof::Claimed
        }),
        client_addr: id.client_addr().map(|a| a.to_string()),
    }
}

/// An SMB session or a local process, as the identity resolver named it.
/// Nobody named (an anonymous session, a process that exited first) is
/// `unknown`.
pub fn resolved_actor(r: &ResolvedIdentity) -> Actor {
    if r.uid.is_none() && r.sid.is_none() {
        return Actor::unknown();
    }
    Actor {
        via: match r.via {
            Via::Nfs => ct::ActorVia::Nfs,
            Via::Smb => ct::ActorVia::Smb,
            Via::Local => ct::ActorVia::Local,
        },
        uid: r.uid,
        sid: r.sid.as_ref().map(|s| s.as_str().to_owned()),
        sec: r.sec.map(|s| match s {
            Sec::Sys => ct::Sec::Sys,
            Sec::Krb5 => ct::Sec::Krb5,
            Sec::Krb5i => ct::Sec::Krb5i,
            Sec::Krb5p => ct::Sec::Krb5p,
            Sec::Ntlm => ct::Sec::Ntlm,
            Sec::Kerberos => ct::Sec::Kerberos,
        }),
        proof: Some(match r.proof {
            Proof::Proven => ct::Proof::Proven,
            Proof::Claimed => ct::Proof::Claimed,
        }),
        client_addr: r.client_addr.clone(),
    }
}

/// `(ino, generation)` from a file handle's bytes: xfs's 64-bit handle
/// (type 0x81/0x82: the inode then the generation, host order) or a 32-bit
/// one (types 1/2). None for any other layout.
pub fn key_of_handle(handle_type: i32, bytes: &[u8]) -> Option<Key> {
    match (handle_type, bytes.len()) {
        (0x81 | 0x82, n) if n >= 12 => Some((
            u64::from_ne_bytes(bytes[..8].try_into().ok()?),
            u32::from_ne_bytes(bytes[8..12].try_into().ok()?),
        )),
        (1 | 2, n) if n >= 8 => Some((
            u32::from_ne_bytes(bytes[..4].try_into().ok()?) as u64,
            u32::from_ne_bytes(bytes[4..8].try_into().ok()?),
        )),
        _ => None,
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn smb(uid: u32) -> Actor {
        Actor {
            via: ct::ActorVia::Smb,
            uid: Some(uid),
            sid: Some(format!("S-1-5-21-1-2-3-{uid}")),
            sec: None,
            proof: Some(ct::Proof::Proven),
            client_addr: None,
        }
    }

    #[test]
    fn hints_are_found_by_the_object_first_then_by_name() {
        let mut h = Hints::default();
        h.note(HintOp::Delete, None, Some(((5, 1), "a")), None, smb(1));
        h.note(
            HintOp::Delete,
            Some((9, 2)),
            Some(((5, 1), "b")),
            None,
            smb(2),
        );
        // By the object, whatever the name.
        assert_eq!(
            h.peek(HintOp::Delete, Some((9, 2)), Some(((5, 1), "zz")), None),
            Some(smb(2))
        );
        // By the directory and name when the object isn't known.
        assert_eq!(
            h.peek(HintOp::Delete, Some((10, 1)), Some(((5, 1), "a")), None),
            Some(smb(1))
        );
        // Another op, or another generation of the directory: nobody.
        assert_eq!(h.peek(HintOp::Rename, Some((9, 2)), None, None), None);
        assert_eq!(
            h.peek(HintOp::Delete, None, Some(((5, 2), "a")), None),
            None
        );
    }

    #[test]
    fn the_latest_rename_to_this_name_wins() {
        let mut h = Hints::default();
        h.note(HintOp::Rename, Some((9, 1)), None, Some("b"), smb(1));
        h.note(HintOp::Rename, Some((9, 1)), None, Some("c"), smb(2));
        assert_eq!(
            h.peek(HintOp::Rename, Some((9, 1)), Some(((5, 1), "b")), None),
            Some(smb(1))
        );
        assert_eq!(
            h.peek(HintOp::Rename, Some((9, 1)), Some(((5, 1), "c")), None),
            Some(smb(2))
        );
        // A name no hint gave: not this one.
        assert_eq!(
            h.peek(HintOp::Rename, Some((9, 1)), Some(((5, 1), "d")), None),
            None
        );
        // The latest change of the owner, by the object.
        h.note(HintOp::Attrib, Some((9, 1)), None, None, smb(3));
        h.note(HintOp::Attrib, Some((9, 1)), None, None, smb(4));
        assert_eq!(
            h.peek(HintOp::Attrib, Some((9, 1)), None, None),
            Some(smb(4))
        );
    }

    /// Codex review of #25 (P2): A's chown hint is used by A's op and never
    /// again. B's later chown, whose hint was lost, is `unknown`, not A's:
    /// the hint is used up, and one noted before B's change is too old.
    #[test]
    fn a_hint_is_used_once_and_never_for_a_later_change() {
        let mut h = Hints::default();
        let t0 = SystemTime::now();
        h.note_at(
            HintOp::Attrib,
            Some((9, 1)),
            Some(((5, 1), "f")),
            None,
            smb(1),
            Instant::now(),
            t0,
        ); // fmt: skip
        // A's chown: found by the object, and used up from both indices.
        assert_eq!(
            h.take(HintOp::Attrib, Some((9, 1)), None, Some(t0)),
            Some(smb(1))
        );
        assert_eq!(
            h.take(HintOp::Attrib, Some((9, 1)), Some(((5, 1), "f")), None),
            None
        );
        assert!(h.is_empty());
        // A's hint again, kept this time (A's op found it but didn't take it):
        // B's change 2 s later can't take it, by object or by name.
        h.note_at(
            HintOp::Attrib,
            Some((9, 1)),
            Some(((5, 1), "f")),
            None,
            smb(1),
            Instant::now(),
            t0,
        ); // fmt: skip
        let b = t0 + Duration::from_secs(2);
        assert_eq!(
            h.take(HintOp::Attrib, Some((9, 1)), Some(((5, 1), "f")), Some(b)),
            None
        );
        // B's own hint, noted after B's change, is B's.
        h.note_at(
            HintOp::Attrib,
            Some((9, 1)),
            None,
            None,
            smb(2),
            Instant::now(),
            b + Duration::from_millis(5),
        ); // fmt: skip
        assert_eq!(
            h.take(HintOp::Attrib, Some((9, 1)), None, Some(b)),
            Some(smb(2))
        );
        // The kernel's coarse clock may put the change a tick after its hint.
        h.note_at(
            HintOp::Attrib,
            Some((9, 1)),
            None,
            None,
            smb(3),
            Instant::now(),
            b,
        );
        let tick = b + Duration::from_millis(4);
        assert_eq!(
            h.take(HintOp::Attrib, Some((9, 1)), None, Some(tick)),
            Some(smb(3))
        );
    }

    #[test]
    fn hints_expire_and_a_flood_is_dropped() {
        let mut h = Hints::default();
        let (t0, w0) = (Instant::now(), SystemTime::now());
        h.note_at(HintOp::Delete, Some((9, 1)), None, None, smb(1), t0, w0);
        assert!(
            h.pick(HintOp::Delete, Some((9, 1)), None, None, t0)
                .is_some()
        );
        let later = t0 + HINT_TTL + Duration::from_secs(1);
        assert!(
            h.pick(HintOp::Delete, Some((9, 1)), None, None, later)
                .is_none()
        );
        for i in 0..(HINT_MAX as u64 + 10) {
            h.note_at(HintOp::Delete, Some((i + 1, 1)), None, None, smb(1), t0, w0);
        }
        assert!(h.len() <= HINT_MAX);
        // A zero inode (an unknown handle) is never a key.
        let mut z = Hints::default();
        z.note(
            HintOp::Delete,
            Some((0, 0)),
            Some(((0, 0), "a")),
            None,
            smb(1),
        );
        assert!(z.is_empty());
    }

    #[test]
    fn nfs_and_resolved_actors() {
        use omnifile_fsal_proto::wire::Identity;
        let mut id = Identity {
            uid: 20002,
            sec: 1,
            addr_family: 4,
            ..Identity::default()
        };
        id.addr[..4].copy_from_slice(&[10, 0, 0, 7]);
        let a = nfs_actor(&id);
        assert_eq!(
            (a.via, a.uid, a.sec, a.proof, a.client_addr.as_deref()),
            (
                ct::ActorVia::Nfs,
                Some(20002),
                Some(ct::Sec::Sys),
                Some(ct::Proof::Claimed),
                Some("10.0.0.7")
            )
        );
        id.sec = 4;
        assert_eq!(nfs_actor(&id).proof, Some(ct::Proof::Proven));
        id.uid = Identity::UNKNOWN_UID;
        assert!(nfs_actor(&id).is_unknown());
        assert_eq!(
            key_of_handle(0x81, &[7, 0, 0, 0, 0, 0, 0, 0, 3, 0, 0, 0]),
            Some((7, 3))
        );
        assert_eq!(key_of_handle(1, &[7, 0, 0, 0, 3, 0, 0, 0]), Some((7, 3)));
        assert_eq!(key_of_handle(0x4d, &[0; 12]), None);
    }
}
