# WP5: the real-cloud harness

Branch `claude/m6-real-cloud-harness`. Everything ran on this Mac against fakes only. No
real Azure, AWS, Microsoft 365 or GitHub account was touched, and no credential was used.

## What it is

`tests/real/run.sh {plan,up,exit,checks,collect,down,all} --cloud azure|aws|tenant [--dry-run]`.
Once the owner supplies the accounts, one command proves what needs a real account and then
removes what it made. The header of `run.sh` lists every option.

- **plan** checks the signed-in account against the allow-list, then fixes the run id and the
  expiry. For Azure it pins the run to that subscription in a private az config directory
  (`OUT/az-config`). Before the approval it makes only three things: an empty, tagged resource
  group (what-if needs one), the PostgreSQL admin group and the registry. Then it runs the
  guide's own `plan=yes` steps and its `gate=whatif` step. The digest that what-if prints is the
  one the owner approves. The guide deploys `docs/guide/customer.bicepparam`; the harness's
  test values (`OMNI_TEST_*`, agreed with WP7 on #33) reach it through `omnifile.env`: run
  tags, Burstable PostgreSQL, a purgeable vault, 1 api replica, and a 1-day unlocked WORM share.
  AWS and the tenant print the harness's own plan (`evidence/plan.json`) and its digest.
- **up** runs only with `--approve <digest>` equal to the plan's. Azure then runs the
  customer guide's `guide-step=N` blocks exactly as written, each on the host its `on=` names.
  Right after the gate step, if the guide's what-if now prints another digest, it stops before
  the deploy. Before the first step that needs one, it brings up a test VM in the
  VNet: a nested-virt `Standard_D8s_v5`, a KVM host for the fresh appliance qcow2, a Samba AD
  DC in a container, and the Linux NFS and SMB client. AWS makes tagged buckets (Standard,
  Glacier, Deep Archive and Intelligent-Tiering objects, with awkward keys), an EventBridge
  rule to an SQS queue, IAM roles, a Roles Anywhere trust anchor and profile, a VPC with eight
  interface endpoints, and an EC2 probe reached only through SSM. The tenant run adds a
  partner probe application.
- **exit** is the M6 exit on Azure (17 steps, `harness/m6exit.py`), through public interfaces
  only:
  - the appliance enrolled through the guide;
  - NFSv3, NFSv4.2 and SMB (as an AD user, with Kerberos) writes;
  - uploaded, then stubbed (the blocks drop on the share);
  - a read back over all three;
  - a pin to Archive, then Azure reports the blob in the Archive tier;
  - a High-priority recall and its read-back;
  - a WORM share, WORM turned on after a fresh sign-in, an overwrite and a delete, both
    versions kept, and Azure refusing to delete them;
  - the R3 gate (`appliance/release.sh`) on the VM's KVM.

  The portal runs in Playwright (`tests/real/portal/driver.mjs`) through an SSH SOCKS tunnel
  over the test VM. The first action opens a headed browser, and the owner signs in to Entra
  there; the harness never sees a password. On the tenant, the exit is the M4 exit. The owner
  confirms what only a person can see (the card), and the harness checks the service side.
- **checks** runs each automated line for that cloud and writes the manual ones to
  `OUT/manual-steps-<cloud>.txt`.
- **collect** writes `results.json` and ticks each line that passed. A real run ticks
  `docs/milestones/real-*-checks.md`, with the run id and its evidence copied to
  `tests/real/results/<run id>/`. A dry run ticks copies in `OUT/docs/` only.
  `tests/real/collect.py --record ID=pass --note ... [--evidence FILE]` records a manual step.
- **down** tears down by tag, WORM first, and proves nothing tagged remains (details below).
- **all** runs plan, up, exit, checks, collect, then down. Down always runs once anything was
  made, even after a failure.

The owner's flow:

```
# write tests/real/allow-list.local (shape: tests/real/allow-list.example); az login; aws sso login
tests/real/run.sh plan --cloud azure                       # prints the run id R and digest D
tests/real/run.sh up   --cloud azure --run-id R --approve D [--appliance FILE] [--r3-dir DIR]
#   stops at each later deploy's what-if (phase 2, AWS settings) and prints its digest D2:
tests/real/run.sh up   --cloud azure --run-id R --approve D --approve D2   # resumes there
tests/real/run.sh exit|checks|collect|down --cloud azure --run-id R
#   (or `all --run-id R --approve D` at a terminal: it asks for each later digest)
tests/real/run.sh all  --cloud aws   (the same two steps)
tests/real/run.sh all  --cloud tenant --azure-run <an Azure run that is up>
tests/real/run.sh down --cloud azure --run-id R --expired  # also sweeps any expired run
```

## Safety: each item and its test

| Item | How | Test (`tests/real/tests/`) |
|---|---|---|
| Only allow-listed accounts | Every phase starts with the identity call (`az account show`, `sts get-caller-identity`) and refuses (exit 4) a subscription, tenant or account not listed. A real run reads only `tests/real/allow-list.local`, refuses it if git tracks it, and refuses `--allow-list`. The file is git-ignored (`tests/real/.gitignore`); the harness never writes it. | `test_safety.py`: 4 refusals with exactly one call in the transcript, 5 phases, a missing list, a committed list, 6 malformed lists, never written |
| Dry runs touch nothing real | PATH shims first, with `/opt/homebrew/bin` dropped (this Mac has real `az`, `aws` and `gh` there). `AZURE_*`, `AWS_*` and `GH_*` are stripped, and the CLIs' config dirs are empty. The run refuses unless az, aws, gh, ssh, scp, docker and node all resolve to the shims. | `test_safety.py` (2 tests) |
| Tags | `omnifile-run`, `omnifile-expires` (fixed at plan, capped by `max-ttl-hours`) and `omnifile-harness` go on every resource group, VM, NSG and ACR, every deployed resource (customer.bicepparam's tags, from `OMNI_TEST_RUN_ID` and `OMNI_TEST_RUN_EXPIRES`), and every AWS resource, IAM role and job. Entra apps carry Graph tags. | `test_tags.py`: every create in the Azure and AWS transcripts; every resource the fake deployment made |
| What-if gate | `up` refuses (exit 3) without `--approve`, and with a digest that doesn't match a fresh what-if. A dry-run `all` may approve its own digest, and logs that; a real run never does. | `test_gate.py`: 3 clouds × no approval and a wrong digest; a stable digest that deploys; another run, another digest; a malformed digest |
| Teardown, WORM first, proven | First the run's locks, the unlocked container policies, legal holds, and each blob version's policy and hold; a **locked** policy is reported, never forced. Then the groups, the custom role definitions scoped to them, a vault purge, and the Entra apps and groups. Then a proof: no group, resource, role definition, soft-deleted vault or app carries the tag. AWS: jobs cancelled, the probe, the endpoints and the VPC, the rule and queue, the buckets (every version), Roles Anywhere, IAM, then a proof through the service listings and the tagging API. Resources the guide's own steps made are adopted (a before/after diff) and removed too; nothing that existed before is ever adopted. | `test_teardown.py`: WORM order; the fake refusing a group delete while WORM data is held; a locked policy left alone; other runs and untagged groups untouched; `--expired`; a purge-protected vault reported |
| A failure mid-run | `--inject-failure STEP` fails at any named step; `REAL_FAKE_FAIL=<regex>` fails a single CLI call inside a step. | `test_teardown.py`: 11 Azure, 7 AWS and 3 tenant injection points, and a failed `az vm create`; each teardown is clean, checked in the fake's own state as well as by the harness's proof |

## The safety review of #36 (2026-11-03): each finding, its fix and its test

Every test runs against the dry-run fakes and is built from the reviewer's repro. All are in
`tests/real/tests/`.

| Finding | Fix | Test |
|---|---|---|
| B1: Entra apps, the Teams catalog app and the partner probe app leaked after a mid-run failure, while the proof still said "nothing tagged remains" | Ids are recorded the moment an object exists (register.sh's entra.json; the partner app at create). Teardown deletes by id, by the run's names ("OmniFile <prefix> portal", `<prefix>-…`) and by tag, and Teams apps by recorded id and by the package's manifest id. The proof counts all of them, tagged or not. The group is found by name as well as by tag, because a guide step can strip its tags | `test_teardown.py`: 12 Azure injection points; a failing `az rest --method patch` (Azure and tenant); a group with its tags stripped. `left_behind` now counts untagged apps and Teams apps |
| B2: identity checked but not pinned | Azure: a private `AZURE_CONFIG_DIR` per run (`azpin.py`), copied from the owner's sign-in, with `az account set` to the checked subscription. AWS: the allow-listed `AWS_PROFILE` and `AWS_REGION` go into every process, guide steps included, keys in the env are dropped, and the account is checked again right before the guide's steps. The Azure run never fills `OMNI_AWS_*` (section 17 skips itself), and `REAL_AWS_RUN` is gone and stripped | `test_review.py`: another shell's `az account set` can't redirect a run; AWS steps see the profile and a fresh check; `aws_values()` stays empty; dry runs drop `REAL_AWS_RUN` |
| B3: the approved what-if wasn't what the guide deploys | The owner approves the guide's own `gate=whatif` digest, and up stops after the gate if it changed. Test values reach customer.bicepparam through `omnifile.env`. Soft-deleted vaults are found by tag and by the names seen in the group, purged when allowed, and reported otherwise | `test_gate.py`: the digest is the guide's; a changed customer.bicepparam stops up before the deploy step; a guide without a gate is refused. `test_review.py`: the test values reach the parameters |
| S4: `plan`, then `all --run-id R --approve D`, never deployed | `all` reuses an approved plan. The PostgreSQL admin group is made once per run. The what-if stub hashes the parameters with every env read resolved, as WP1's compiles them | `test_gate.py::test_plan_then_all_with_its_digest_deploys` |
| S5: WORM teardown was blind on real az | Each version is read with `blob show --version-id`, and container holds with `legal-hold show` (the fake's `list` now shows neither, as az 2.90's doesn't). Data-plane 403s are retried while the role propagates. A held account no longer stops the rest: everything else in the group goes one by one, and the account is reported | `test_teardown.py`: WORM order through `blob show`; 3 injected 403s; a locked policy leaves only its account |
| S6: a real tenant run took a dry Azure run's results | `azure:` exit results count only from a run of the same kind that came up. collect refuses results of the other kind | `test_review.py` (2 tests) |
| S7: AWS teardown stopped part way | Each part is isolated. Endpoints are waited for before subnets and security groups. A bucket with no tags (`NoSuchTagSet`) still goes when its name is the run's | `test_teardown.py`: a failing `events.DeleteRule`; an untagged guide bucket |
| S8: a run id read from tags went into OData filters | Run ids are strict (`rr-<yyyymmddhhmm>-<4 hex>`, or `rr-dry-NNNN`) everywhere: `--run-id`, `--azure-run`, and `--expired` tags, which are skipped when malformed. A `down` with no state works | `test_review.py` (5 tests); `test_teardown.py`: an evil tag never reaches a filter; down with no state |
| S9: AWS adoption could take over others' resources | Adopted only when named for the run (its id or prefix); anything else is logged and left alone | `test_teardown.py::test_the_aws_run_adopts_only_what_is_its_own` (the fixture makes "someone-elses-anchor") |
| Minor | The aws shim refuses without moto. Goldens now show policies, UserData and the parameters' env whole, with only random ids normalised. The SSH tunnel is up before the browser starts, which fixes the flaky order. `--r3-dir` uploads the build and the gate runs; `--ad-realm` text is gone. Exit-step evidence is copied with the line. secrets.json is 0600 from creation. Enrolment codes stay in secrets.json only, masked in every log. The probe role is scoped: buckets, queue, and `iam:PassRole` to the batch role only | `test_review.py` (shim, probe policy, tunnel order, r3, secrets); goldens |

## The re-review of #36 (at 272c138): each finding, its fix and its test

Each test is in `tests/real/tests/test_rereview.py`. Each was checked by reverting its fix in
a scratch copy of the worktree, where the test fails (the scratchpad's `verify.py`: 14
reverts, all caught).

| Finding | Fix | Test |
|---|---|---|
| 1: teardown by name deleted other runs' objects. The 5-hex prefix collided (`rr-202611030004-0043` and `rr-202611030009-009e` were both `rc3d36`), and `--expired` did the same | The prefix is now `r` and 7 hex: 8 characters, WP1's `namePrefix` limit. Names the harness picks itself carry the whole run id: `ofreal-<run id>-pg-admins`, `ofreal-<run id>-partner-probe`, and the AWS roles `ofreal-<run id>-…`. A name match counts only when the object carries no other run's tag and was made inside the run's window, from its plan to now (`ownership.py`). Anything else is logged and left. An expired-run sweep goes by tag only. AWS adopts a name only when it holds the whole run id as a word of its own | the colliding pair no longer collides; an app with another run's tag, an untagged app from before the run, and an expired run's untagged app all survive `down --expired`, while the run's untagged in-window app goes; AWS adoption by run id, not prefix |
| 2: the az sign-in copy kept tokens, was made before the allow-list check, was never deleted, and `--out` could point into the repo | Every phase first reads the owner's sign-in where it is and checks it. Only then does it copy the profile and the MSAL token cache, plus the service principal entries for an SP sign-in, and pin the copy. The copy is deleted when the phase ends: main's `finally`, with SIGTERM and SIGHUP turned into that path, and a stale copy is removed before a new one is made. An `--out` inside the repository is refused unless it is under the gitignored `tests/real/out` | no az call uses a copy when the check refuses; the copy is gone after a phase that passes and one that fails; only the needed files are copied; 3 `--out` paths are refused |
| 3: phase 2 and the AWS settings deployed on the guide's approval only | Every `gate=whatif` step is a gate. The plan's digest covers the first deploy. Each later gate's digest is approved with its own `--approve` (repeatable), or pasted at a prompt in an interactive run. Otherwise `up` stops there (exit 3), with that what-if in `evidence/whatif-step-N.txt` and the resume command. A resumed `up` starts at that step. A guide where a `whatif.sh --deploy` step has no gate since the last deploy is refused. The fixture has a phase-2 gate (steps 18 and 19). WP7's guide marks steps 14, 41 and 76 (#33 at ecf63e5) | stops at step 18 with only the first digest, deploys nothing more, and resumes with both; a wrong second digest deploys nothing; an ungated deploy is refused |
| 4: B2's test didn't switch mid-phase | The fake can switch the owner's default in the middle of a phase (`REAL_FAKE_SWITCH_AFTER`), and it records the subscription every changing call acted in | a switch right after `group create` in plan, and after `vnet create` in up: every call acts in the planned subscription |
| 5: a LOCKED policy was held forever | A version whose locked protection has ended (its own policy's expiry, or its container's locked days from its creation) is deleted and recorded. A container with a locked policy and nothing left under retention is deleted, so the account goes too. Protection still in force is held, and the report says until when | an expired locked version and an expired locked container: deleted, no residue; a version locked until 2027-01-01: held, with its date |
| 6: goldens recorded omnifile.env only as a byte count | The transcript records every value the parameters read (`OMNIFILE_*` and `OMNI_*`) and every line of each env file copied to the edge. Values whose names look secret are redacted | the transcript and the golden hold `OMNI_USER_CIDRS=10.60.1.0/24`, so a widened range fails the golden |
| 7: `ad_realm` was never written, so the tenant stub-read guard never fired | The Azure run records its realm when it sets up the DC | a dry Azure run's state has the realm, and a real tenant run's stub read reports pending without running anything |

## The third pass on #36: each finding, its fix and its test

Each fix was checked by reverting it in a scratch copy of the worktree. All 5 reverts failed
their tests. A control copy with no revert passes the same tests, so each failure comes from
its revert.

| Finding | Fix | Test |
|---|---|---|
| Should-fix: an `all` that paused at a later gate was torn down. `down` cleared the pause, and the printed resume then hit ResourceGroupNotFound | `all` pausing at a later gate (no terminal to approve at) leaves the run up and prints `all --run-id R --approve D --approve D2`. That command reuses the plan, resumes at the gate and goes on to the teardown. `up` refuses a run that is down, and a fresh `plan` clears the down mark | `test_gate.py`: the paused `all` deletes nothing, and its own resume command completes; `up` after `down` is refused before any call, then works after a new plan |
| Minor: dry runs read the owner's HOME (the Teams schema cache) | The schema fixture (above); `--schema` in the fixture guide; HOME, XDG_CACHE_HOME, XDG_CONFIG_HOME and UV_CACHE_DIR under `OUT/dry/`, and UV_OFFLINE | `test_rereview.py`: the dry env and its seeded cache; the fixture is build.sh's pinned schema; an Azure dry run on an empty HOME passes and writes nothing there |
| Minor: only a one-line `whatif.sh … --deploy` counted as a deploy | Continuation lines are joined first, and a direct `az deployment group/sub/mg/tenant create` counts too. WP7's guide at #33 still passes: gates 14, 41 and 76, no ungated deploy | `test_rereview.py`: a continued `--deploy`, a `group create` and a `sub create` with no gate are each refused |

## T5: the direct-replica check on the TCP path

RA-30 is now written for `apiIngress = tcp`, the default on PR #34. The check runs a probe
inside `api`'s own replica with `az containerapp exec`: any container in the internal subnet
will do, `web` can scale to zero at `deploymentSize=small`, and `api` always has a replica. The probe connects to `api`'s TCP
ingress (`apiUrl`, port 8443) twice with no client certificate: once plainly, once with a
forged `X-Forwarded-Client-Cert`. The line passes only if both are refused, either at the
TLS handshake or with a 4xx. It fails if either gets in, and stays partial if `apiUrl`
doesn't name 8443. The VM-side RA-12 makes the same two attempts from the VNet.
`test_checks.py` covers the verdicts. The doc line also lists what only a deployment shows:
the TCP ingress and its NSG rule reach `api` from the edge networks and the relay, and the
IP restrictions apply to TCP.

## Every line is mapped

All 117 checkbox lines now carry an id (`RA-01`–`RA-42`, `RW-01`–`RW-32`, `RT-01`–`RT-43`).
`test_mapping.py` fails if a line has no id, if an id has no entry, or if an entry names a line
that doesn't exist.

| | automated | an exit step | recorded manual step |
|---|---|---|---|
| Azure (42) | 17 | 2 | 23 |
| AWS (32) | 18 | 0 | 14 |
| Tenant (43) | 6 | 15 | 22 |

The manual lines need a person, a pilot, days of waiting (an inventory, an expiry, 90 days in
Archive Access), a locked WORM policy, a second cloud at once, or the owner's release vault.
Each says what to do and what to keep as evidence (`harness/checks/*.py`).

## The first real AWS run: its policy and the cheap pass

The owner's first AWS run uses the IAM user `omnifile-harness-user` (account
111122223333, us-west-2), which has AmazonS3FullAccess, AmazonEC2FullAccess, ViewOnlyAccess
and SecurityAudit. Two files cover the rest (how to use them: `tests/real/aws/README.md`):

- **`tests/real/aws/harness-roles.yaml`**, a CloudFormation template the owner reviews and
  deploys once. It makes the batch role, the probe's role and instance profile (fixed
  policies over `ofreal-rr-*` buckets and queues), and two permissions boundaries.
- **`tests/real/aws/harness-policy.json`**, the user's policy (attached as two managed policies since 2026-11-12: IAM refused it inline, at 8,500 characters against a user's 2,048). The user can't change
  the template's roles, and may pass them only to their own service (batch to S3 Batch
  Operations, probe to EC2). The only role it can make is one under `/omnifile-real/` that
  carries the edge boundary, which allows reading `ofreal-rr-*` objects and nothing else.
  Boundary changes are denied. Tags the policy trusts (`omnifile-*`) can be set only when a
  resource is made: an explicit Deny covers `ec2:CreateTags` outside a create,
  `rolesanywhere:TagResource`, the tagging API, and tagging queues, rules or roles that
  aren't the run's. `ssm:SendCommand` needs the instance's `omnifile-run` tag, and
  `kms:Decrypt` needs an S3 encryption context of an `ofreal-rr-*` bucket.

Roles Anywhere checks `rolesanywhere:TagResource` as well when a create carries tags, so the
policy allows it under the creates' own conditions (the run's three tag keys and values). IAM
can't tell that from tagging an anchor that already exists, so the run's tags could be put on
an owner's Roles Anywhere resource and then let it be deleted or its CRL replaced, never its
roles reached. The README says so, has the owner check the account has no Roles Anywhere
resources first, and gives `aws iam simulate-custom-policy` commands for the create path and
the forged-tag path, which `test_aws_policy.py` reproduces.

The review of #56 found that the first version let the user make a role, trust it to
itself, give it `*:*` and assume it; pass any probe-named role to EC2; and forge tags onto an
instance or a Roles Anywhere anchor. `test_aws_policy.py` now evaluates those routes against
the policy's own statements and fails if any is allowed. It also checks that what the run
needs is still allowed, that every API call in the golden and cheap transcripts is covered,
that the template's roles stay inside their boundary, and that a run never writes to the
owner's roles.

`--scope cheap` is the cheapest run that keeps the checks meaningful: six interface endpoints
instead of eight (no STS or S3 Control; the probe has no internet, so SSM's three stay), a
t3.nano probe (the AL2023 AMI the run takes is x86, so not t4g) terminated once its checks
are done, 5 MiB parts and no 5.2 GiB object (RW-02 is pending), Bulk restores only, one
Batch Operations restore wave instead of two, and a 3-hour expiry unless `--ttl-hours` says
otherwise. The scope is in the plan's digest, and a run planned cheap refuses to continue as
full. Every AWS plan prints an estimated cost from list prices (`evidence/plan.json`): the
cheap pass is about $0.57 for an hour, most of it the two Batch Operations jobs at $0.25 each.

The dry run's AWS region is now us-west-2 too, so the goldens match the real run's.

A test run deploys the guide's small size (`OMNI_DEPLOYMENT_SIZE=small`, #59) and overrides
none of its sizes: the B2ms server and the api and worker replica counts are gone from
`guidevalues.TEST_VALUES`, so a run gets small's B1ms server, 32 GB and replica counts.

## Results (2026-11-03)

- `uv run --project tests/real pytest tests/real/tests -q`: **312 passed** (23 min, after merging main at 8e43ab6). It ran with the fake az models for WP7's whole guide, on a clean temporary HOME with no AZURE_*, AWS_*, ARM_* or GH_* set, and left nothing in that HOME. The Bicep CLI itself crashed now and then (SIGABRT in an HTTPS call during `build-params`), so a dry run runs a crashed bicep again, up to five runs a second apart (`core.RETRY_ON_CRASH`); an error is not retried.
- `uvx ruff@0.8.6 check tests/real` and `format --check`: clean.
- `tests/real/run.sh all --dry-run --cloud azure --allow-list tests/real/fakes/allow-list.dry`: exit 0. The plan ran fixture steps 1, 3 and 4
  (the gate). Up ran 1–16 and the phase-2 gate and deploy (18, 19), with 8 and 11 recorded for the owner, and the M6 exit's 17 steps
  passed (the R3 gate is pending without `--r3-dir`). Checks: pass 18, partial
  1, manual 23, fail 0. Teardown: no residue.
- `--cloud aws`: exit 0. Checks: pass 7, partial 11, manual 14, fail 0. Teardown: no residue.
  The partial lines are ones moto can't show: TLS, the signing helper, restores that finish.
  They answer partial rather than pass.
- `--cloud tenant`: exit 0. Checks: pass 13, manual 25, pending 5 (the `azure:` exit steps,
  without an Azure run). Teardown: no residue.

A dry run proves the harness's control flow and the exact calls it makes (the golden
transcripts in `tests/real/golden/`), not Azure's or AWS's behaviour.

## Fakes and stubs

- `tests/real/fakes/bin/{az,aws,gh,ssh,scp,docker,node}` → `fakecli.py`. Every call goes to
  the transcript, and the tests compare it with the goldens. `az` keeps a small model:
  groups, the deployment's resources and outputs, role definitions, soft-deleted vaults,
  Entra apps, and WORM containers and versions. It refuses what Azure refuses where it
  matters (a group delete with WORM data held, a purge under purge protection), and an az
  command it has no model of fails. `aws` goes through boto3 to the run's moto server.
- moto 5.2.5 in server mode, so the harness and the aws shim share one state. moto lacks Roles
  Anywhere, S3 Control jobs and SSM output; `fakes/aws_extra.py` answers those at botocore's
  `before-call`. It also models two S3 behaviours moto gets wrong: 409 on a second
  `RestoreObject`, and a non-MD5 ETag under SSE-KMS. `fake_az.py` and `gh-releases-stub.py`
  (appliance) didn't fit: they serve Key Vault signing and release downloads, which the
  harness doesn't call.
- **WP1's scripts are the real ones** (PR #34, on main since 68aedc0). The stubs are gone:
  dry runs and real runs both run `deploy/azure/whatif.sh` (with the Bicep CLI, which compiles
  main.bicep and the parameters), `deploy/entra/register.sh` and `deploy/teams/build.sh` as
  written, against the fake az. A run refuses if one is missing. For them the fake az models
  register.sh's Entra calls: `ad app list/update`, `ad sp show/create` (Graph's scopes and
  roles), `ad app permission`, `federated-credential`, the worker's `appRoleAssignments`, and
  the Teams catalog's `appDefinitions`. teams/build.sh validates against Microsoft's manifest
  schema, pinned at @microsoft/app-manifest 1.1.3 (MIT). The schema ships as a fixture,
  `tests/real/fakes/fixtures/teams-schema/1.1.3/`: the fixture guide passes it with `--schema`,
  and a dry run seeds its own cache with it, which covers WP7's guide too. A dry run has its
  own HOME, XDG cache and uv cache under `OUT/dry/`, with `UV_OFFLINE=1`, so it reads nothing
  from the owner's home and never downloads. It needs `service/.venv` synced
  (`uv sync --project service`, as the service lane does), because teams/build.sh runs
  `uv run` there.
  The fixture guide imports the released images by digest (`az acr import`), as WP7's guide
  does. `deploy/images/build.sh` is OmniFile's own build step, not a customer's. Its
  customer.bicepparam assigns every parameter main.bicep requires.
  The harness sets the env that test.bicepparam reads: `OMNIFILE_TEST_PREFIX` (the run's
  prefix becomes the deployment's `namePrefix`), `_PG_ADMIN_CIDRS` and `_UPLOADER_OID`.
  Before what-if it writes `relay-allowed-cidrs.json` with `relay-ranges.sh`, because
  test.bicepparam loads that file. It reads main.bicep's outputs by their names.
- **The customer guide**: `docs/guide/` is on main (#33). `run.sh all --dry-run --cloud azure`
  (no `--guide`) runs WP7's whole guide, all 82 steps: the workstation's run here, the VM's,
  console's and portal's go to their fakes, and the manual and owner-only ones are recorded.
  Run against the guide, the dry run has the same counts as with the fixture, and the teardown
  leaves nothing. The tests still use `tests/real/fakes/guide/` for the golden transcripts, and
  `tests/test_guide_models.py` runs the whole guide too. What the guide needs beyond the
  fixture:
  - **Fake az models** (`tests/real/fakes/guide_models.py`): `ad group show` and
    `member check` (the groups Entra Connect syncs, as a real tenant has them), `acr show`,
    `acr import` and `acr repository show` (by digest), `containerapp list` and `revision list`,
    `identity show`, `keyvault show` and `secret show`, `role assignment list` and `delete`
    (and the Key Vault Secrets Officer role a phase-1 deployment grants its uploader),
    `network vnet show` and `peering`, `network private-dns zone` and `link vnet`, and
    `network private-endpoint` with its `dns-zone-group`. Register.sh's app roles and
    assign-app-roles.sh's `appRoleAssignedTo` are modelled too. The models refuse what Azure
    refuses. A show of what doesn't exist is ResourceNotFound. A zone link needs its zone and
    VNet. A private endpoint needs a real target and the right group id. A peering is
    Initiated until the other side exists. A role is assigned only once.
  - **The other tools that would reach a network** go to the fakes too: `psql` and `curl` are
    shims; `openssl s_client` and the service's `uv run ... catalog.migrate` and `admin` are
    intercepted, while every other `openssl` and `uv` call runs for real.
    `tools/check-private-dns.sh`, which asks this machine's resolver about real Azure names,
    is replaced in the dry run's copy of the release by a stand-in. The stand-in resolves a
    name privately only once its endpoint, zone group and zone link to the hub exist, as on
    a real workstation. A dry run takes an OpenSSL 3 or later, never LibreSSL, as the guide's checks now do.
  - **Offline Bicep:** the dry run's `bicep` has a dead proxy (`HTTPS_PROXY` and `HTTP_PROXY`
    `http://127.0.0.1:9`, `NO_PROXY` empty), so it reaches no network. Bicep 0.48 makes
    several HTTPS calls to aka.ms on every `build` and `build-params`, whatever bicepconfig
    says. A local listener saw 4 to 8 per build, with the analyzers off and with no module
    aliases too; `--version` makes none. Its .NET HTTP stack still aborts now and then
    (an AccessViolationException), even on the refused proxy. In one suite run it crashed
    three times in a row. A local 403 proxy doesn't cure it either (2 crashes in 80, against
    4 in 80 behind the refused one). So a crash is retried, up to five runs in all, a second
    apart, and an error is not.
  - **The az guard** (`harness/azguard.py`) is first on the PATH of every guide step, in
    real runs as in dry runs. It is a safety net against accidental deploys, not a security
    boundary: a step runs as the same user as the harness and can always reach the real az or
    ARM if it sets out to. The text checks above stay as an early warning.
    - **What it refuses:** `deployment … create` at any scope, `group deployment create`,
      `stack … create/set`, `resource` calls on a deployment or stack (by id, URL, or
      `--namespace Microsoft.Resources --resource-type deployments`), and `rest` PUT, PATCH
      or POST (or a body with no method) to any `/deployments/` or `/deploymentStacks/` URL.
      %-escapes are decoded until nothing changes. It sees argv after the shell, so quoting,
      variables, `eval`, `env` and `bash -c` change nothing.
    - **The one exception:** `deploy/azure/whatif.sh`'s own create, after its digest check.
      The harness gives each approved deploy step a one-shot grant for its gate's digest, the
      run's group, and the compiled template and parameters that gate previewed (whatif.sh
      prints their hashes before the digest). whatif.sh names the digest it checked
      (`OMNIFILE_DEPLOY_APPROVED_DIGEST`). All must match, with one `--template-file` and one
      `--parameters @file`, with nothing after the file (az would read `name=value` there
      as an override on top of it). A granted create may carry only the options whatif.sh
      passes, matched exactly, each once (`-g`, `-n`, `-f`, `-p`, `-o` or their long forms):
      az accepts abbreviations such as `--param`, so any other option is refused. The grant
      is spent under an exclusive lock (`flock`).
    - **Its own paths are fixed:** the harness writes the run's wrapper (`OUT/guard/az`) with
      the real az, the grants file and the log as the guard's own arguments. No step's
      environment names them, and nothing a step sets moves them.
  - **Only whatif.sh deploys, and only with its own gate's fresh digest.**
    - A guide step that deploys any other way is refused, at plan time and again before it
      would run: `az deployment … create` at any scope (and the older `az deployment create`
      and `az group deployment create`), `az stack … create/set`, or New-Az…Deployment.
      Only `deploy/azure/whatif.sh --deploy` binds the approved digest.
    - Before a deploy step runs, its own gate (the nearest one before it) must have
      previewed a digest in this run that the owner approved. The `OMNI_WHATIF_DIGEST` the
      step will use must be exactly that digest. A digest carried over from an earlier gate
      is refused.
    - When the step's gate printed its skip marker, the deploy step is not run at all. In
      WP7's guide that is section 17 without AWS values.
    - The re-review of #56 found the hole: a gate printed a skip marker and recorded nothing,
      then a direct `az deployment group create` ran on phase 1's digest. Its three variant
      guides (g57-1 to g57-3) are now each stopped before any second deploy.
  - **A gate that prints no `digest:` line** counts as empty only when the guide prints its
    skip marker (`...: section 17 skipped`) and the recorded digest is unchanged. Otherwise
    the run stops there. The review of #56 found the hole: a step 18 that wrote its what-if
    to a file printed nothing, yet still recorded a fresh digest that the phase-2 deploy
    then used. The test replays that exact mutation, and the same mutation with a fake skip
    marker.
  - **Default deny for the network.** `nslookup`, `dig`, `host`, `wget`, `nc`,
    `ssh-keyscan` and `telnet` are refusing shims. `git fetch`, `clone`, `ls-remote`,
    `pull`, `push`, `remote` and `submodule` are refused, while local git runs. openssl's
    `s_time`, `s_server`, `ocsp`, `cmp` and `ech` are refused; only `s_client` is modelled.
    Every proxy-honouring client is pointed at the dead proxy, with NO_PROXY left only for
    the local moto server. psql refuses an SQL file it doesn't model, and curl answers 404
    for a path it doesn't know.
  - The Azure golden records the env files the VM gets by their normalised values, no longer
    by their size, which held the checkout's absolute paths.
  The run also supplies two values. `OMNI_HUB_VNET_ID` is a stand-in hub: an empty, tagged
  VNet in the run's group, because a test run has no client hub. `OMNI_NOTIFY_SENDER` is the
  owner's own sign-in. The appliance goes in `appliance-files/`, with its `SHA256SUMS`.

## The local rehearsal (pending on WP2)

`tests/real/rehearsal/m6-local.sh` (`--check` is the default and starts nothing) and
`m6_local.py` drive the same exit locally: the arm64 appliance under HVF with the setup
answers as credentials, `dev/m4/stack.py`, and the shared omnifile-dc VM (joined only). It
has not run. `--run` refuses until WP2's arm64 image and HVF support in
`appliance/tests/vm.sh` are on main; a test checks that it refuses before starting anything.
No VM was started for this package.

## What's left

- **L** Run the local rehearsal once WP2 merges.
- **L** WP7's guide now has `gate=whatif` on steps 14, 41 and 76 (#33 at ecf63e5), and its
  lint, like the harness, refuses a deploy with no gate since the previous one. Section 17's AWS
  names take `OMNI_AWS_NAME_SUFFIX` (#33 at 3cd6a16; empty for clients). The AWS run sets it
  to the run id, so the roles, trust anchor, profile, queue and rule its steps make are named
  for the run, and adopted and removed by it. The IAM OIDC provider isn't suffixed (AWS keys
  it by issuer URL), so a real AWS run that makes one leaves it, logged as not adopted: the
  owner removes it by hand if it was made for the test.
- **P** Every automated line, the M6 exit and the M4 tenant exit need the owner's accounts.
  To check on the first real run:
  - that a Dv5 VM with `--security-type Standard` gives `/dev/kvm`;
  - that the wizard offers itself on the serial console (`vm/console-answers.py`);
  - that the private DNS zones reach the test VM (the guide links them to the hub);
  - that `az containerapp exec` gives usable output with no TTY.
- **O** The owner writes `tests/real/allow-list.local`, signs in, and approves each digest. The
  signed R3 gate needs the release vault (`--r3-dir` plus a `SIGNER` file on the VM). The
  tenant exit needs an AD synced to Entra: give the Azure run that AD (the run's own DC
  isn't synced).

## Tools added (pinned in `tests/real/uv.lock`, test-only)

`tests/real/` is its own small uv project (`tests/real/pyproject.toml`), so the service's
lockfile is untouched. Run it with `uv run --project tests/real pytest tests/real/tests -q`.
- boto3 1.43.123 with `[crt]` (awscrt 0.38.0, for CRC64NVME): Apache-2.0. The real AWS run uses it too.
- cryptography 50.0.2: Apache-2.0 or BSD. It makes the test CA and certificates.
- moto 5.2.5 with only the services the harness calls (`s3, s3control, sqs, iam, ec2,
  events, ssm, sts, kms`), plus flask 3.1.3 (BSD-3-Clause) and flask-cors 6.0.5 (MIT) for its
  server mode: Apache-2.0. Dev group, dry runs only. `moto[server]` is no longer used: it
  pulled in cfn-lint and, through it, `regex` (Apache-2.0 AND CNRI-Python), which
  `release/licences.py` doesn't allow. The narrower set is 36 packages instead of 67, and the
  tagging API still works without cfn-lint.
- pytest 9.2.0: MIT. Dev group.
- ruff 0.8.6 through `uvx`, for lint only, not in the lock (pinned in `release/tools.toml`,
  named in `tests/real/run.sh`).

`LICENSES.md` has rows for each direct dependency of `tests/real/uv.lock`: boto3, awscrt,
cryptography, moto, flask, flask-cors and pytest, in the Python table. `release/licences.py`
reports no problem in the tests-real SBOM. Its other 42 problems are the appliance's `deb`
and `generic` rows. Only an appliance SBOM from a real build, with its source offer, matches
those rows, and there is no appliance build here.
