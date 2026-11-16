# Decision 64: a filer that doesn't show its ACLs is flagged, and new grants are blocked there

Branch `claude/annex-acls-hidden`, stacked on decision 65's `claude/annex-filer-access`, which is stacked on decision 63's. The migration is `0054_filer_acls_hidden.sql` for now; it takes the next free number at merge.

## What was built

- **The edge checks at every mount.** A mount report that could read the annex root carries `acls_shown`: whether the server shows the ACL attribute of the annex's flavour there. That is the POSIX ACL over NFSv3, the NFSv4 ACL, or the NT descriptor over SMB, answered even with "none" (`annex/acl.rs`, `acls_shown`).
- **The service flags the filer** (`filer.acls_hidden_since`) from the first report of `false`, and raises an `acls_hidden` problem once. A later report of `true` clears the flag and its acknowledgement.
- **The acknowledgement:**
  - `PATCH /v1/filers/{id}` `{acls_hidden_acknowledged: true}`;
  - an ArchiveAdmin, freshly signed in, with If-Match;
  - only while flagged;
  - kept with who and when, and audited as `filer.acls_hidden`.
- **Until it is acknowledged:**
  - placements and share copies of the filer's files that anyone but the owner could read fail `acls_hidden_filer`;
  - portal names and recalls go on;
  - `Filer.acls.blocks_grants` says so, and the gauge `omnifile.annex.filers_acls_hidden` keeps the alert `annex-filer-acls-hidden` up.
- **The portal** shows "The NAS shows its ACLs" beside the annex's filer: the flag, since when, and Acknowledge for an ArchiveAdmin.
- **"Group-readable"** is a read grant to anyone but the file's owner (a group, a named user, or Everyone): the coordinator's reading, applied to the staged ACL at `go` and to the share copy's ACL.
- **Which filer:** the one the file was captured from, since that is where its captured read set may be wider than the NAS's.

## Tests

Lanes at 70660a30:

| Lane | Result |
|---|---|
| Annex exit test on omnifile-acl | 308 passed, 0 failed, in 9933 s, at 58011f13 (before the review fixes) |
| Edge, Linux | `cargo test -p omnifile-edge -p omnifile-control` 469 passed in 18 suites; `cargo fmt --check` and clippy clean |
| Service | 2684 passed, 50 skipped, 0 failed, own Postgres 17, `PYTHONHASHSEED=6401` |
| Ruff | check and format clean; `deploy/azure/check.sh` 34 ok (one run hit a crash inside `bicep` itself, and passed when run again) |
| Portal | `npm test` 333 passed in 39 files; typecheck, lint and `check:client` clean |

**New tests:**
- **Edge:**
  - every mount report says whether the server shows its ACLs, and not when it can't be read;
  - a local filesystem shows POSIX ACLs and no NT descriptor.
- **Service:**
  - a NAS that shows no ACLs is flagged at its mount, said once, unchanged by a report that can't tell, and cleared with its acknowledgement when it shows them;
  - a flagged filer places only what its owner alone reads;
  - an acknowledged flag lets it go on;
  - "group-readable" is anyone but the owner;
  - a flagged filer makes no group-readable share copy until acknowledged;
  - an ArchiveAdmin acknowledges: only while flagged, the owner gets 403, it is kept with who did it, and audited;
  - the gauge.
- **Portal:** the flag beside the filer, and Acknowledge sending the PATCH.
- **Exit test:** in each annex's suite, the mount report says the server shows its ACLs.

**Fixes undone in turn: 15, each caught.**
- **Service, 12:**
  - a mount flags;
  - it is said once;
  - a mount that shows clears;
  - the clear takes the acknowledgement with it (caught by the check constraint too);
  - placements refuse;
  - share copies refuse;
  - an acknowledged flag lets it go on;
  - "group-readable" is anyone but the owner;
  - the acknowledgement only while flagged;
  - it is audited;
  - who acknowledged;
  - the gauge.
- **Edge, 3:**
  - every mount says;
  - the flavour's attribute;
  - not when the mount can't be read.

**What this check can't see:** a server that answers but withholds entries, or a mixed-style volume. It is to confirm on a real NAS, as decision 64 says.

## Review fixes

| Finding | Commit | Fix |
|---|---|---|
| Codex on #84, for 64's Acknowledge too: a `reauth_required` refusal showed only its text | dbff13c2 | `ErrorCallout`, which offers "Sign in again". The test fails without it. |
| Re-review of #85: a report without `acls_shown` (an edge from before 64), or null from a mount whose root wouldn't open, left the filer unflagged | cfaa6491, f1b6e50a | Fail closed: either flags the filer; only null from a failed mount changes nothing. The edge now always sends the field, null when it couldn't look, so a report without it is an older edge's. Tests for each case and the edge's; 3 undoings caught (2 service, 1 edge). Older tests' mount reports now carry the field, as the edge sends it. |

Lanes at f1b6e50a (decisions 63 and 65 with their review fixes merged in):

| Lane | Result |
|---|---|
| Annex exit test on omnifile-acl | `annex_a1_test.sh` 308 passed, 0 failed, in 9857 s |
| Edge, Linux | 472 passed in 18 suites; fmt and clippy clean |
| Service | 2703 passed, 51 skipped, 1 error: `test_t5.py`'s setup, while the exit test's build loaded the machine; that file then passed 15 of 15 alone |
| Ruff | check and format clean |
| Portal | 335 passed in 39 files; typecheck, lint and `check:client` clean (at dbff13c2; no portal change since) |
