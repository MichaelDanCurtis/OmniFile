# WP8: setup wizard rerun, and registering storage backends

Branch `claude/m6-setup-rerun-backend-cli`. Two defects the customer guide (WP7) found.

## 1. The console wizard couldn't rerun after a failure

**What happened.** The first run formats the data disk and mounts it at `/srv/omnifile/data`. A rerun (the console unit restarts setup after any failure) then listed that disk as mounted, and the disk question ended setup with "/dev/vdb is in use (it holds the system)". So a filer whose AD join failed looped at the console. The same refusal hit every console rerun after a finished setup, so `sudo omnifile-setup` could never change an answer. The rerun test missed it because its `/dev/vdb` had no mount.

**The fix** (`edge/crates/omnifile-setup/src/prompt.rs`):
- The questions recognise the data volume an earlier run prepared, and only when all of these hold: the xfs mounted at the data root is on that disk (same UUID), nothing else of the disk is mounted, its label is `omni-data`, and setup's own `srv-omnifile-data.mount` names that UUID.
- That disk is offered to keep ("Keep it as the data disk and go on? [Y/n]"), and setup goes on to the AD questions. No `ERASE` is asked and nothing is formatted. Declining stops with nothing changed.
- Any other mounted disk (the system disk, another label, another UUID, or the system mounted from it too) is never kept. Typed by hand, it is refused, naming where it is mounted, and the question is asked again instead of ending setup.
- A disk that holds data is still formatted only after `ERASE` is typed.

**A second defect, found by the unattended test** (`src/steps.rs`): when the data step found the volume already mounted, it never cleared `erase`. An unattended credential with `erase = true` is read again at every boot, so the rerun saved `erase = true` into `/etc/omnifile/setup.toml`, where a later `omnifile-setup apply` would act on it. The step now clears it whenever the volume exists.

**Tests** (each fails on origin/main):
- `tests/prompt.rs::a_rerun_after_a_failed_join_keeps_the_data_disk_and_joins`: a first console run formats and mounts the disk and stops at a failed join; the rerun offers the disk, asks no `ERASE`, and its steps join with no `mkfs` or `wipefs`. On main: `Err("/dev/vdb is in use (it holds the system): choose an empty disk")`.
- `a_rerun_that_declines_the_data_disk_changes_nothing`, `a_mounted_disk_setup_did_not_make_is_never_kept_or_offered` (three cases), `the_system_disk_typed_by_hand_is_refused_and_asked_again`.
- `tests/steps.rs::an_unattended_rerun_after_a_failed_join_keeps_the_data_disk_and_joins`: first boot with `erase = true` formats and fails at the join; the next boot (credential still asking to erase) reuses the mounted volume, joins, and saves `erase = false`; with the volume not yet mounted, its label still keeps it. On main it fails at "the saved answers never ask again".

`docs/design/setup-wizard.md` describes the rerun and lists the tests.

## 2. No supported way to register a storage backend

**What exists.** Nothing writes `catalog.backend` outside tests and `dev/`. No migration seeds it, no deploy job does, and the portal and partner API only read it (`GET /v1/backends`; an inline S3 source only finds a row an ArchiveAdmin made). `catalog/privileges.py` gives no app INSERT or UPDATE on it, so only the catalog's administrator can write it.

**What's built:** `python -m omnifile_service.admin backend add|list|show` (`service/src/omnifile_service/admin/`).
- **Connection** (`admin/db.py`): `catalog/connect.py`'s `from_env`, with all its checks, as the administrator: no `-c role=` of an app, and with Entra sign-in the administrator's own token (`AZURE_CLIENT_ID`'s managed identity, else `DefaultAzureCredential`). With `OMNIFILE_DATABASE_URL` the string is used as it is, with the token in `PGPASSWORD`, as the guide runs the migrations.
- **Azure** (`add azure`): `--live A --archive B` (two accounts, Smart on by default) or `--account A [--worm]` (one account, WORM shares). Account and container names, the region and the backend name are checked. The pair must be two accounts; Smart on one account, and WORM with two, are refused. The row goes through `AzureBlobConfig.from_mapping` and `capabilities_for`, as the worker builds it. The config is the one the guide's SQL wrote, plus `region` when given.
- **S3** (`add s3`): `--account`, `--region`, `--buckets` (at least one). The row is checked against the service's own AWS settings (`OMNIFILE_AWS_ACCOUNTS_FILE` or `OMNIFILE_AWS_ACCOUNTS`, with the audience) by `validate_backend_config` and `S3Config.for_backend`: the account must be configured, in that region. A source that is one of OmniFile's operations buckets is refused (decision 40).
- **Private networking** (decision 22): rows are private by default. An S3 row can't be public when the AWS settings are private (the default). An Azure row can be public only with `OMNIFILE_DEV_MODE=1`. A private row is never made public.
- **Idempotent**: found by name; the same answers again print `unchanged` and write nothing. Different answers update a row nothing uses yet. Once a dataset (home or source), a source or a migration uses it, only S3 buckets may be added, plus an Azure row's region. A row never changes kind.
- Every add and update is audited (`system:admin`, `backend.add` or `backend.update`, with the database user and the config before).
- Answers are checked before connecting. Exit 0 done, 1 refused, 2 no catalog configured.

**Tests** (`service/tests/admin/`, 45, against the shared Postgres):
- the pair's row is what the worker and api build, files land in the live account, and an archive move goes to the archive account;
- the same answers again change nothing, and the row the guide's SQL made is already as asked;
- a row under data never moves; private networking never turns off;
- a WORM account; S3 against the AWS settings file, found by a partner's inline source (`resolve_inline`);
- every refusal above, an app's role refused (`permission denied`), an unmigrated catalog, `python -m`, and the Entra connection without an app role.

## 3. Review fixes (independent review of #37 at c2df4da)

Each fix has a test that fails on c2df4da. I checked that by running it against that commit's source.

1. **Should-fix: a partition of the system disk was formatted with no ERASE.** This bug predates WP8. On a one-disk appliance, `/dev/vda4` (the inactive `/usr` slot B: unformatted, unmounted, GPT label `_empty`) passed both the questions and the data step. It was formatted, so the shares would have lived where the next update writes.
   - Both now go through one check, `steps::data_device`. It resolves the device first (`realpath -e`, so `/dev/disk/by-path/...` names its disk), then requires `lsblk -dno TYPE` to say `disk`, and refuses the device if anything of it, or of its children, is mounted.
   - The data step formats the resolved device.
   - Tests: `prompt.rs::a_partition_of_the_system_disk_is_never_the_data_disk` adopts the reviewer's one-disk machine and also covers the same answer in a file with `erase = true`. `the_system_disk_typed_by_hand_is_refused_and_asked_again` now also covers `/dev/vda4`, its by-path alias, the system disk's by-path alias, and a missing device. `steps.rs::the_data_disk_is_a_whole_disk_never_a_partition_by_any_name` covers the unattended path by name and by alias, and checks that a by-path alias of a whole disk formats that disk.
2. **A credential's `erase = true` could wipe another disk on a later boot.** Once setup's mount unit names a data volume, `data_device` refuses any disk whose UUID differs, whether or not `erase` is set. This is stricter than asked. It also covers an empty disk, because formatting one in the volume's place would leave the replica pointing at files that aren't there. The message says to reattach the volume's disk and run setup again, or to deploy the appliance again. There is no shell before setup finishes, so "remove the unit" was no way out (re-review). No console override was added: with the volume's disk missing, setup can't tell whether it held files. Test: `steps.rs::once_setup_made_the_data_volume_no_other_disk_is_formatted_in_its_place` (another disk with data, and an empty one).
3. **Concurrent identical `backend add`s.** On `ON CONFLICT DO NOTHING`, `register` now runs the `SELECT … FOR UPDATE` again and continues to the compare. Test: `test_a_concurrent_add_of_the_same_name_is_compared_not_refused`. The first add is held uncommitted, and the second is seen waiting on its lock. With the same answers the second prints `unchanged`; with different ones it prints `updated`.
4. **The audit named only the group.** The record now also keeps the token's `oid`, `upn`, `appid` and `tid` (`admin/db.py::signed_in_as`). The token comes from the credential, the connection string, or `PGPASSWORD`, in libpq's order. The token itself is never stored. Tests sign in for real to the shared server as a throwaway login whose password is an Entra-shaped fake token, which is dropped afterwards. They cover the Entra way (`OMNIFILE_PG_HOST`, a fake credential) and the guide's way (`PGPASSWORD`), plus unit tests of the claims.

## Lanes run

| Lane | Result |
|---|---|
| `cargo test -p omnifile-setup` | 53 passed (19 unit, 11 prompt, 23 steps) |
| `cargo clippy -p omnifile-setup --all-targets -- -D warnings` | clean |
| `rustfmt --edition 2024 --check` on the crate's files | clean |
| `uv run pytest -q tests/admin` | 51 passed |
| `uv run ruff check` and `ruff format --check` (admin files) | clean |
| `uv run pytest -q` (full service suite, `OMNIFILE_TEST_DATABASE_URL` on 127.0.0.1:55433) | before the review fixes, with no other suite on the server: 1295 passed, 50 skipped, 0 failed. After them: 1297 passed and 4 failed, then 1294 passed and 7 failed (see below) |

**The full-suite failures after the review fixes are not this change's.** Two other packages' suites ran on the same Postgres throughout: the PR #34 reviewer's, and worktree `agent-a50045e5c92ae4229`'s.
- Every failure is a test that reads the catalog feed: `api/test_feed_and_changes.py`, `api/test_m5_exit.py`, `api/test_upload_flow.py`, `catalog/test_feed.py`, `web/test_partner_events.py`, `web/test_partner_reads.py`.
- The feed's horizon is `pg_snapshot_xmin(pg_current_snapshot())`, which is cluster-wide, so another database's open transaction holds it back.
- Which tests fail differs from run to run. Each passes when rerun: `api/test_upload_flow.py` had 3 failures, then 14 of 14 passed on the next run.
- No app imports `omnifile_service.admin`, and the edge crate isn't in this suite.

The data step now also runs `realpath` and `lsblk -dno TYPE` on the box. `appliance/tests/setup.sh` was not rerun (it needs a VM, not this package's), so its first check on the real image is still to come.

No new tools or dependencies. `azure.identity` and `psycopg` were already the service's.

## For the lead: the guide's replacement steps (PR #33)

Not applied here (`docs/guide/` is PR #33's).

**Section 7.3, step 26.** Replace the code block with:

````markdown
```bash guide-step=26 on=workstation
. ./omnifile.env && . ./omnifile.state.env
PGPASSWORD=$(az account get-access-token --resource-type oss-rdbms --query accessToken --output tsv) \
OMNIFILE_DATABASE_URL="host=$OMNI_PG_HOST dbname=$OMNI_PG_DATABASE user=$OMNI_PG_ADMIN_GROUP sslmode=verify-full sslrootcert=system" \
  uv run --project service --no-dev python -m omnifile_service.admin backend add azure \
  --live "$OMNI_LIVE_ACCOUNT" --archive "$OMNI_ARCHIVE_ACCOUNT" --region "$OMNI_LOCATION"
```

Success: one line, `added: azure (backend 1, azure_blob, private network): live <live account>,
archive <archive account>, Smart, <region>`, or `unchanged: ...` on a rerun. The portal offers it as
"azure" when you add a share. The api and worker read it when phase 2 deploys them (section 9).
````

**Section 17.4, step 71.** Replace the `PGPASSWORD=... psql ... register-s3-backend.sql` command (after `state.sh set-file`) with:

```bash
PGPASSWORD=$(az account get-access-token --resource-type oss-rdbms --query accessToken --output tsv) \
OMNIFILE_DATABASE_URL="host=$OMNI_PG_HOST dbname=$OMNI_PG_DATABASE user=$OMNI_PG_ADMIN_GROUP sslmode=verify-full sslrootcert=system" \
OMNIFILE_AWS_ACCOUNTS_FILE=aws-accounts.json OMNIFILE_AWS_WEB_IDENTITY_AUDIENCE="$OMNI_AWS_AUDIENCE" \
  uv run --project service --no-dev python -m omnifile_service.admin backend add s3 \
  --account "$OMNI_AWS_ACCOUNT_ID" --region "$OMNI_AWS_REGION" --buckets "$OMNI_AWS_SOURCE_BUCKETS"
```

Its success line is `added: s3-<account> (backend N, s3, private network): account <account>, <region>, buckets <buckets>`. The row is checked against `aws-accounts.json` as the worker will check it, so a bucket that is `OMNI_AWS_OPS_BUCKET`, or a region other than the account's, is refused before anything is written. Steps 72 and 73 then redeploy api and worker, which read it.

Then delete `docs/guide/sql/register-azure-backend.sql` and `register-s3-backend.sql`, and the two tests in `docs/guide/lint/test_tools.py` that read them (lines 224 and 234 on PR #33's head), or point them at the CLI.
