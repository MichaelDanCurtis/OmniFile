# WP7 results: customer guide, Windows kit, legal drafts

Branch `claude/m6-customer-guide`, from origin/main at 0810ac4. 2026-11-03.

## What is built

**The customer guide** (`docs/guide/README.md`): an Azure admin follows it from nothing to a working
share, in 18 sections and 82 steps. Prerequisites (people and rights, networks, the workstation, the AD
POSIX attributes `uidNumber`, `gidNumber` and `loginShell`, with a tool for an AD that has none), the
values file, images, the Bicep deploy in two phases, the private endpoints and DNS the customer creates
(decision 22), the database (principals, migrations, the storage accounts as the catalog's backend), the
Entra app registration and Teams app, the edge CA and the relay's certificate, the appliance on Hyper-V
(VHDX), vSphere (OVA) and KVM (qcow2), the console wizard, enrolment from the portal, the first dataset
and share, checks (NFS and SMB writes, a refused user, uploads, a stub read back, a pin to Archive, a
High-priority recall with the fail-fast open and the Teams card, versions), troubleshooting, and the AWS
side for migration clients. It gives the Kerberos cost (about 53% fewer IOPS with krb5, 77% with krb5p)
and promises no recall time above 10 GB. It documents `nolock` for NFSv3 clients, since the edge runs no
NLM.

Every command or input is a fenced block `<lang> guide-step=N on=<where> [manual=yes] [input=NAME]
[wait=N]`, so WP5's harness runs each step as written. Workstation steps load `omnifile.env` (the
customer's values, from `docs/guide/omnifile.env.example`) and `omnifile.state.env` (what earlier steps
recorded). Other steps use `<value:NAME>`. The format and the variable names were agreed with WP5.

Supporting files in `docs/guide/`:
- `customer.bicepparam`: the deployment's parameters, read from those two files with
  `readEnvironmentVariable`, so nobody edits a parameters file; the images come by digest from
  `deploy/images/images.lock.json`.
- `tools/`: `check-env.sh` (the values' shapes, and that no allow-list covers the relay subnet),
  `state.sh`, `outputs.sh`, `check-private-dns.sh`, `tls-issuer.sh` (the enrolment pin),
  `enrol-exec.sh` (runs a command in one `enrol` replica and cuts the PEM it prints),
  `assign-app-roles.sh`, `run-step.sh` (runs a workstation step exactly as written), and
  `Set-OmniFilePosixIds.ps1` (reports, then hands out, POSIX ids in AD; never changes one set).
- `sql/`: the managed identities' database roles, and the Azure and S3 backend rows.
- `aws/`: the service role (web identity from Entra) and Batch role policies, written from the S3, S3
  Control and SQS calls the service makes; `render.sh` fills them and the edge policies from
  `deploy/aws/iam/`; `accounts-json.sh` composes `OMNIFILE_AWS_ACCOUNTS`.

**The guide lint** (`docs/guide/check.sh`): compiles `main.bicep` offline and resolves every step fence
(unique, increasing numbers; known places, languages and keys), every `OMNI_` value, every Bicep
parameter and output (including every camelCase name in the prose), every repo path, script flag,
`python -m omnifile_service` module and subcommand and `omnifile-setup` subcommand, psql variables
against the SQL files, portal routes and labels against `portal/src`, setup answers against
`answers.rs`, the required sections, the Kerberos note and the 10 GB rule. It builds
`customer.bicepparam` for phase 1 and phase 2, and runs shellcheck on the guide's scripts.

**The Windows client kit** (`tests/windows/`): a runner for each matrix row (`rows/W01.ps1` to
`W14.ps1`), `Invoke-Matrix.ps1` with `matrix.example.psd1`, and the edge-side recall counter
(`edge/omnifile-recall-count.sh`, which counts the daemon's `recall started` events in a window). Each
row writes `Wnn.json` and a line of `results.md` in the matrix's columns. `docs/pilots/windows-client-matrix.md`
says how to run it on site.

**Legal drafts** (`docs/legal/`), each headed "DRAFT for counsel review. Not legal advice. Not in
effect.": an individual CLA, an entity CLA, the DCO alternative with its trade-off (it gives no right to
sell commercial licences), and questions for counsel (contributions, the BSL Additional Use Grant, the
GPL-3 `samba-vfs/`, the LGPL-3 `fsal/` and installation information on a read-only verified `/usr`, Arc
agent redistribution, the GPL source offer of decision 46). `CONTRIBUTING.md` says outside contributions
are not accepted until an agreement is in effect. `README.md` matches `AGENTS.md`'s current state.

## Lanes run (all offline, on this Mac), after the review round

| Lane | Result |
|---|---|
| `docs/guide/check.sh` | ok: 82 steps, nothing pending (WP1 and WP8 merged), both phases of `customer.bicepparam` build; shellcheck 12 scripts ok |
| `docs/guide/check.sh docs/guide/lint/fixtures/broken-guide.md` | exit 1, 130 problems of 32 kinds (most are the example values the fixture doesn't use) |
| `docs/guide/test.sh` | 61 tests OK: the lint (green on the guide, red on the fixture for every rule, nothing pending, a pending entry that resolves fails, a pending script's undeclared flag fails, a parameter main.bicep lacks fails, a parameters file that doesn't build fails), every tool against a fake `az`, a local TLS server and the service's own config parsers, and the guide's own steps run as written against fakes (peering, the database zone and forwarder, the secrets, the vault right, the signing key, the S3 notifications, the `[aws]` answers, every database connection with a group name that has a space) |
| `tests/windows/run-tests.sh` | Pester 5.7.1: 64 passed, 0 failed, 0 test files failed to run (every row, pass and fail cases; the module; the recall counter against a fake journalctl and a fake root; `Invoke-Matrix.ps1`; the AD tool against a fake AD; the runner fails on a file that throws at discovery) |
| `docs/legal/check.sh` | 5 drafts checked, all headed |

Two bugs in WP7's own tools were caught by the first tests and fixed: `aws/render.sh` filled an unset
value with nothing (so a policy could name no bucket), and the AD tool failed under strict mode on an
attribute AD leaves out.

## The independent review of #33 (at e433de0), and what changed

Every item is fixed in its own commits on the branch, each with a test or a fixture case that fails
without the fix where one can be written.

1. **PostgreSQL resolvable from on-premises** (blocking): step 18 links the database's private zone to the
   hub, step 21 adds a `postgres.database.azure.com` forwarder, and section 16 has a row for it. Test:
   the steps run against a fake `az`.
2. **Checks from hosts the allow-lists admit:** WP1 added `adminAllowedCidrs` (web and enrol admit the
   admin network), which `customer.bicepparam` sets from `OMNI_ADMIN_CIDRS`, and `check-env.sh` refuses
   an admin range that covers the relay subnet. The web and enrol checks and the enrolment pin run on the
   workstation; `api` is checked from a new place, `on=edge-network`.
3. **The `[aws]` answers are replaced, not appended:** an idempotent edit, with root reads; a rerun gives
   the same file byte for byte (tested three times over).
4. **`ssh -t`** for sudo over ssh (step 52 and the matrix doc).
5. **`tls-issuer.sh` fails closed:** it verifies the chain and the name (`-verify_return_error
   -verify_hostname`) and writes no pin otherwise. Tests: an untrusted chain and a wrong name each exit 1
   and write nothing. The workstation now needs OpenSSL 3, which step 6 checks (macOS's LibreSSL has
   neither Ed25519 nor the name check).
6. **S3 notifications merged:** step 74 reads each bucket's settings, adds EventBridge and writes them
   back; the warning is before the block. Test: a bucket's SNS notification survives.
7. **The Pester runner fails on a file that fails to load:** it exits 1 unless Pester's result is
   Passed. Test: a fixture that throws at discovery.
8. **The recall counter refuses to count blind:** as neither root nor a member of `systemd-journal` or
   `adm`, it needs the unit's entries to be visible, else it exits 1 saying why. Tests: refused, and
   counted as root.
9. **The lint checks more:** commands inside quotes, `expect` lines, OmniFile paths on the filer, unit
   names, `OMNIFILE_` settings, and the flags of scripts and modules still to come (declared in the
   pending files, from WP1's and WP8's branches); a label from a template needs at least five characters
   of fixed text, and no more than twice as much made-up text. One fixture case each. The new flags check
   found that `deploy/teams/build.sh` needs `--portal-url`, which step 27 now gives.
10. **Fallback email:** step 28 runs `register.sh --mail-sender --worker-client-id`, and the new step 29
    is the Exchange Online RBAC for one mailbox (`Application Mail.Send`, scoped). `Mail.Send` is no
    longer claimed as a Graph grant.
11. **A group name with a space:** every libpq string quotes `user='...'`; `check-env.sh` refuses quotes
    and backslashes in the name. Test: each connection string in the guide parsed as libpq does.
12. **The card signing key is made once:** step 37 does nothing once `OMNI_INTENT_KID` is recorded. Test:
    a rerun after the key file is gone changes nothing.
13. **Keys off the command line, and no standing vault right:** the lease and cursor keys go through a
    0600 file; phase 2 names no uploader, and step 43 removes the Secrets Officer role phase 1 granted.
    Tests: no `--value` and no file left; the role deleted when present.
14. **A hub without a gateway stops step 20,** unless `OMNI_TEST_HUB_NO_GATEWAY=1` (test runs only;
    `check-env.sh` accepts only empty or 1 and says it is for tests). Tests: stops, test override, gateway.

Also in this round: WP8's backend CLI (PR #37) replaces the guide's SQL for the backend rows (steps 26
and 75), the SQL files and their two tests are gone, and the wizard-rerun workaround is removed, since
#37 fixes the rerun. WP1's apps now start not ready rather than restarting before phase 2, and section
5.3 says so.

## The re-review of #33 (at 287f683)

13 of 14 were confirmed fixed; item 9 and six minors got another round, each with a fixture case or test
that fails without it:
- **Templates:** a label from a portal template must have each variable part be a value the guide names
  (a file it made, a `<value:NAME>`) or a string the portal quotes, never free text (`Recall ahead of
  tme` now fails).
- **Continuation lines:** `python -m` modules, subcommands and flags are read from the joined, tokenised
  command, so a flag on a `\`-continued line (`--lieve`) or inside a quoted command is checked.
- **Edge paths** match whole path tokens of the sources (or a folder of one, or a copy beside one), so
  `/etc/omnifile/setup.tom` fails.
- **Cross-references:** every "step N" in the prose must exist, and "step N creates/records/... `X`" must
  name a step whose text holds X (this found "Step 1 creates `omnifile.env`": it is step 4).
- **The troubleshooting row** names step 39 for the secrets and the vault role to grant again after step 43.
- **Step 29 (Exchange)** checks before each create, so it is safe to repeat (a Pester test runs the step
  as written, twice, against stand-ins).
- **Step 39** removes its key file on any exit (`trap`); tested with a failed upload.
- **Every edge-shell step** begins with `set -euo pipefail` (the lint requires it), and step 79 writes
  the new `setup.toml` to a temporary file, checks it, then moves it into place; a failing `awk` leaves
  the old file as it was (tested).
- **The rerun text that relies on PR #37** is marked `<!-- pending WP8: setup-rerun-keeps-data-disk -->`,
  which needs its `feature` entry in `pending-wp8.txt`; the entry resolves, and so fails, once
  `prompt.rs` on main holds #37's question.

## For WP5's harness (#36's safety review)

- `gate=whatif` on each what-if that records `OMNI_WHATIF_DIGEST`: step 14 (phase 1), step 41 (phase 2)
  and step 76 (the AWS settings), and `plan=yes` on the read-only steps 9 and 13 before the first. The lint
  requires plan steps on the workstation, before the first gate; every gate on the workstation, recording
  the digest; and a gate between each `whatif.sh --deploy` step and the deploy before it, as WP5's harness
  does (fixture case: a second deploy with no preview of its own).
- Test-only values in `omnifile.env.example` ("Test runs only"), each empty for a client:
  `OMNI_TEST_RUN_ID` and `_RUN_EXPIRES` (the run's tags), `_PG_SKU_TIER`, `_PG_SKU_NAME`, `_PG_STORAGE_GB`,
  `_KV_PURGE_PROTECTION`, `_KV_SOFT_DELETE_DAYS`, `_API_MIN_REPLICAS`, `_API_MAX_REPLICAS`,
  `_WORKER_MAX_REPLICAS`, `_WORM_RETENTION_DAYS` (the WORM share `tst`, unlocked), `_LOCK_WORM`,
  `_BLOB_SOFT_DELETE_DAYS`, `_ALERTS`. `check-env.sh` accepts each only empty or test-shaped and prints a
  test-run note. `customer.bicepparam` falls back to main.bicep's own default for each, which the lint
  checks against the compiled template, and it builds a third time with the test values (the run's tags
  present; a client's build carries none).
- `OMNI_AWS_NAME_SUFFIX` (test runs only; empty, or a run id's shape `^[a-z0-9][a-z0-9-]{3,39}$`): section
  17's AWS names (the three roles, the trust anchor, the profile, the event queue and rule, the endpoints'
  Name tags) end in `-<suffix>`, in the steps, `render.sh` (`NAME_SUFFIX` in the templates) and
  `accounts-json.sh`. Empty, a client's render is byte-identical to before (golden files in
  `docs/guide/lint/fixtures/aws-render/`); the lint fails an AWS name in a step without `$sfx`.
- Step 10 leaves an existing resource group alone (a PUT would replace its tags), and tags a new one
  with the run's three tags in a test run.

## API clients (after PR #43)

Section 18 registers each API client: step 80 gives its application the API's application roles for its
scopes (`assign-app-roles.sh` now takes `app:<application id>` as well as a group), step 81 registers it in
Settings, API clients (client id, name, scopes, owner), and step 82, only for a client that can't use Entra
app credentials, makes it an OmniFile API key. Its values are the four `OMNI_API_CLIENT_*` in
`omnifile.env`, all set or all empty (`check-env.sh`). Troubleshooting has rows for `403
client_not_registered` and `403 owner_lacks_access`. The guide never names a partner product: it says API
client or the API, and the lint fails on such a name anywhere in `docs/guide/`.

## The allowances: none left

The guide was written against WP1's and WP8's branches before they merged, with their names on pending
allowances (`docs/guide/lint/pending-wp1.txt`, `pending-wp8.txt`). WP8's went once PR #37 merged
(4e94a24) and WP1's once PR #34 merged (68aedc0): `check.sh` passes no allowance, and the guide resolves
against main alone (a test checks there is none). The mechanism stays in the lint for a future package,
with its tests on stand-in allowance files. Since WP1's third round, `check-env.sh` refuses any network
that overlaps the relay subnet, as main.bicep's `fail()` does, rather than only one that holds all of it.

## What's left

- **P** WP5's real run of the guide. Things only a real run shows, beyond `real-*-checks.md`:
  whether `az containerapp exec` output can be captured without a terminal (steps 33 and 35; the steps
  say to copy the PEM by hand if not); `sslrootcert=system` against Azure PostgreSQL's certificate on a
  customer workstation (steps 24 to 26); that the second certificate `enrol` presents is the issuer the
  edge pins (step 38); the four DNS forwarders (step 22); the Exchange RBAC (step 29); the Teams admin
  centre path (step 31); the Entra issuer, `aud` and `sub` as IAM sees them, and whether
  `docs/guide/aws/service-role-policy.json` is enough and no more (section 17).
- **P** Hyper-V, vSphere and KVM recipes (steps 48 to 50) on real hosts, and the Windows matrix with the
  kit on site.
- **O** Counsel's review of `docs/legal/`, then the owner's choice of agreement and signing mechanism.
- **O** Where clients download the appliance files and which registry publishes the images
  (`OMNI_SOURCE_REGISTRY`): the guide assumes both come with the release notes.
- **O** A security contact for `CONTRIBUTING.md` (it says "write to AiLuxis Research", with no address).
- **O** NFSv3 locking: the owner turned it on (2026-11-04); the guide mounts with locking. The NLM and statd ports in its troubleshooting row are the ones `rpcinfo` lists, until WP2 gives fixed ones.

## Out-of-scope bugs found

1. **The setup wizard couldn't be rerun on the console once the data disk was set up.** Fixed by WP8
   (PR #37); the guide's workaround is removed.
2. **No supported way to register storage backends.** Fixed by WP8's `omnifile_service.admin` CLI (PR
   #37), which the guide now uses.
3. **The written GPL offer isn't in the appliance image.** `appliance/source-offer/` makes it and
   `publish_sources.py` publishes it, but nothing puts `SOURCE-OFFER.md` into the image
   (`questions-for-counsel.md` 6.4 asks whether it must be there). For WP6 or WP2.
4. **`web` crash-looped until phase 2.** Fixed by WP1: the apps start not ready.

## Tools installed

- Pester 5.7.1 (Apache-2.0), from the PowerShell Gallery with `Save-Module` into
  `~/.cache/omnifile/pester`, not system-wide; `tests/windows/run-tests.sh` pins and fetches it. It is
  test-only, for `tests/windows/`: WP6 please add it to `LICENSES.md`.
- Already present and used: PowerShell 7.5.3, Bicep CLI 0.48.1, shellcheck, LibreSSL 3.3.6 and
  OpenSSL 3.6.5 (Homebrew; the TLS test's server needs it).

`pwsh` is launched through `tests/windows/run-tests.sh`: this session's worktree guard refuses a direct
`pwsh` command.
