# WP1 results: deployable service and T5

Branch `claude/m6-deployable-service`, from origin/main at 0810ac4. Run on the Mac (arm64), 2026-11-03/04.

## What is built

**Entrypoints (the gap found in planning).** `modules/app.bicep` and `modules/job.bicep` take a required `command`, which replaces the image's ENTRYPOINT and drops its CMD. `main.bicep` gives each app its own (`appCommands`): `python -m omnifile_service.api`, `.enrol`, `.web`, `.worker`, the verifier the worker's, and `omnifile-relay`. Before, enrol, the worker and the verifier would all have run `Containerfile.api`'s CMD, which is `api`. `service/tests/test_deploy_env.py` compiles `main.bicep` and evaluates each app's command and settings with a small ARM expression evaluator. Against origin/main's compiled template it reports 12 errors (each of the six apps ignores the command, and its module takes none); mutation tests feed it a wrong command, a container without the command, and an unchecked new app.

It also found that `enrol` had no `OMNIFILE_API_URL`, so an enrolled edge would have sent every control call to `enrol`. Fixed, with a test.

**T5, fixed by default** (`apiIngress = tcp`; `docs/design/threat-model.md` T5, `docs/contracts/control-api.md` §2):
- `api` sits behind a TCP ingress on 8443 and terminates mutual TLS itself (`direct_tls`); an `X-Forwarded-Client-Cert` header is refused.
- Its server certificate is the edge CA's: each replica makes a key in memory (a memfd), `enrol` signs it (`/internal/v0/sign`, `purpose: api_server`, for `OMNIFILE_ENROL_API_SERVER_NAMES` only, EKU serverAuth, 30 days), and `api` renews it in place from day 20.
- The enrolment answer carries `api_server_ca_sha256`; `omnifile-control` pins that CA for `api`, refuses an answer naming a CA it didn't send, and checks `server-ca.pem` against the pin at every start.
- Forwarded mode is no longer offered by the template (review of #34: no safe proxy range exists while `api` shares the internal environment). In code it fails closed: it refuses a proxy range wider than /16 or overlapping the other apps' subnets (`OMNIFILE_API_APP_SUBNETS`).
- The NSG rules and the relay's base URL follow the port.

**Found and fixed on the way** (each with a test that fails without the fix):
- `enrol` opened its Key Vault signer in a throwaway event loop and served in another, so every sign would have failed with "Event loop is closed" (shown with a real httpx client; the test uses a transport bound to its loop). The signer now opens in enrol's lifespan.
- `api` and `enrol` exited without the CA certificate, so the CA bootstrap (`az containerapp exec ... init-kv-ca`) couldn't run in a fresh deployment. Both now wait: `/healthz` 200, everything else 503.

The service image also set `OMNIFILE_API_CLIENT_AUTH=forwarded` by default (no test: Bicep always sets it); removed, so the code's default, `direct_tls`, applies.

**Also built:**
- `python -m omnifile_service.enrol issue-relay-cert --csr-b64 ...`: the relay's client certificate from the vault key, audited, with the SHA-256 for the new `relayCertSha256` parameter.
- Bicep: the Azure Bot as a managed-identity bot on the worker's identity with the Teams channel; `web`'s sign-in settings (`webOrigin`, `webClientId`, `webApiAudiences`); the Teams settings for `api` and the worker (`teamsAppCatalogId`, `relayCertSha256`, `teamsIntentKid`, `teamsSecretsProvisioned`, `notifySender`); `awsAccounts`, `awsWebIdentityAudience`; `keyVaultPurgeProtection`, `keyVaultSoftDeleteDays`, `keyVaultUploaderObjectId`; outputs `apiUrl` (with the port), `apiIngress`, `webOrigin`, `webUrl`, `enrolUrl`, `keyVaultName`, `teamsBotAppId`. The six image parameters are required (no placeholder).
- `deploy/images/build.sh`, `smoke.sh`, `smoke_probe.py`, `images.lock.json`.
- `deploy/azure/test.bicepparam`, `whatif.sh`, `test-whatif.sh`, `testdata/whatif.json`.
- `deploy/teams/build.sh`, `package.py`, `test-build.sh`.
- `deploy/entra/register.sh`, `test-register.sh`, `testdata/fake-az` and two golden transcripts.

## Lanes run

| Lane | Command | Result |
|---|---|---|
| Bicep | `deploy/azure/check.sh` (Bicep CLI 0.48.1) | 29 ok, 0 fail: main.bicep and three parameters files build with no warnings, main.bicep and 17 modules lint clean, closed by default, test.bicepparam's values, relay-ranges.sh (19 checks), whatif.sh (16) |
| what-if | `deploy/azure/test-whatif.sh` | 16/16, a fake `az` |
| Entra | `deploy/entra/test-register.sh` | 17/17, a fake `az`, two golden transcripts |
| Teams | `deploy/teams/test-build.sh` | 11/11, the manifest valid against MicrosoftTeams.schema.json v1.17 |
| Service | `cd service && OMNIFILE_TEST_DATABASE_URL=postgresql://postgres:omnifile@127.0.0.1:55433/postgres uv run pytest -q` | 1333 passed, 50 skipped (backend-contract capability skips, and the opt-in Floci, scale and restore-drill lanes; none is WP1's) |
| Service lint | `uv run ruff check src tests`, `ruff format --check` | clean |
| Relay | `cd relay && uv run pytest -q` | 127 passed |
| Edge | `cd edge && cargo test -p omnifile-control` | 53 passed (28 unit, 4 contract, 15 control, 5 pin_api_server, 1 sealed) |
| Edge lint | `cargo clippy -p omnifile-control --all-targets -- -D warnings`; `cargo fmt -p omnifile-control -- --check` | clean |
| Images | `deploy/images/build.sh --only X`, then `deploy/images/smoke.sh ... --remove`, one image at a time | 3/3 built; 14/14 smoke checks (below) |

New tests in the service suite: `test_deploy_env.py` 38 (was 8), `tests/api/test_t5.py` 12, `test_t5_renewal.py` 10, `test_enrol_api_server.py` 18, `test_enrol_spec.py` 6, `test_verifier_job.py` 3, `tests/web/test_web_waiting.py` 3, and one in `test_security_fixes.py`. Relay: `tests/test_waiting.py` 2.

**Fail without the fix**, as the rules ask:
- `test_deploy_env`'s entrypoint check on origin/main's compiled template: 12 errors (every app ignores the command parameter; its module takes none). The mutation tests (`test_a_wrong_entrypoint_fails`, `test_a_container_without_the_command_fails`, `test_a_new_app_without_a_check_fails`) feed it a wrong one each run.
- T5: with origin/main's template, the api environment is `forwarded` with proxies `10.60.0.0/26`, and a forged `X-Forwarded-Client-Cert` from 10.60.0.37 was accepted as the victim edge's certificate (scratch script over the old template's evaluated environment). `test_the_old_default_is_why` keeps that case in the suite: naming the subnet as the proxy range lets the forgery in. The default and the TCP-passthrough tests refuse it.
- `test_enrol_tells_edges_the_api_url_api_listens_on`: origin/main's enrol has no `OMNIFILE_API_URL`.
- `pin_api_server.rs`: with the client pinning enrol's CA as before (simulated), `api_is_reached_with_the_edge_ca_the_enrolment_answer_names` fails.
- `test_the_key_vault_signer_signs_in_the_serving_loop`: the old open-then-serve path fails with "Event loop is closed" (`test_the_old_way_fails_against_a_loop_bound_vault`; the real failure was first reproduced with a real httpx client across two `asyncio.run` calls).
- `test_enrol_waits_for_the_ca_certificate`, `test_api_waits_without_the_ca_certificate`: origin/main exits instead.

## Images

Built at 652176d (clean; rebuilt after the review round, the first build was at 0b53a9c), linux/amd64, on the Mac's Docker Desktop (29.9.2, buildx 0.38.1, containerd image store) under emulation, each with a throwaway `docker-container` builder removed with its cache afterwards. Nothing was pushed. The images and the `moby/buildkit` image the builders pulled were removed after the smoke runs.

| Image | Reference (registry prefix to add) | Size |
|---|---|---|
| relay | omnifile-relay@sha256:176523d7b2ec088ac717ec0da641332d1f699107d3e20b0149f0e358389846f4 | 51 MiB |
| service | omnifile-service@sha256:b264c1001ac43f553e155fc4d199fa12348e16fa7b0e97926c7e5c8da256b820 | 158 MiB |
| web | omnifile-web@sha256:36794f916b5523d5b441b14b2b94d8924bd328e4c18c894f804d2619f508f04b | 159 MiB |

`smoke.sh`, each app with its command from the compiled `main.bicep`, against a throwaway database on the shared Postgres (127.0.0.1:55433, from the containers as host.docker.internal): migrations; enrol healthy; api healthy; then the T5 path in containers: an edge enrols, the answer pins the edge CA for api, api serves a serverAuth certificate from enrol for its name, a heartbeat over mutual TLS is 200, a forged header is 401, no client certificate gets no request; the worker running after 30 s; the verifier one pass, exit 0; web healthy (dev issuer); relay healthy. 14/14 (`ok` lines).

Free disk: `/` was at 6.4 GiB when the builds were first due, so they waited; across the final builds it stayed between 11 and 15 GiB (`build.sh` refuses to start below 8). At the end: `/` 15 GiB, `/Volumes/Main External` 1.5 TiB.

## Second round (follow-ups and the review of #34)

Follow-ups, each with a test:
- `control-api.yaml` has `purpose: api_server` and `api_server_ca_sha256`. `test_enrol_spec.py` calls every enrol route with each purpose and fails on any answered field the spec doesn't declare, also for api's certificate answers (4 of 6 fail on the previous spec).
- `relay/README.md`: with TCP ingress the relay's CA bundle is the edge CA.
- web, the relay, api and enrol start not ready before phase 2 instead of restarting: one log line names what is missing, `/healthz` answers 503; liveness is the port, readiness `/healthz`.
- The verifier job runs `python -m omnifile_service.worker.verifier`: one pass over the `verify` queue, exit 0 or 1, every 15 minutes by default (`verifierTrigger = Schedule`).
- `adminAllowedCidrs` admits the admins' workstations to web and enrol.

Review findings:
- Renewal could die silently: any failure is now retried with backoff, and api stops (exit 1) if renewal ends or the certificate is within 2 days of its end. `test_t5_renewal.py`, the reviewer's probe among them, fails 10/10 on the previous code.
- No safe `apiIngress = http`: the template allows only `tcp`; in code, forwarded mode refuses a proxy range overlapping `OMNIFILE_API_APP_SUBNETS`.
- Trust lists refuse ranges wider than /16 (/48 for IPv6).
- A TCP startup probe gives api 5 minutes to get its certificate.
- `register.sh` checks every id it puts in a Graph URL or `$filter` against the GUID pattern.
- Edge CA rotation re-enrols every edge: documented as a known limitation with its procedure.
- `tlsproto.py`'s docstring is corrected.

## Third round (the review of #34 at 24f908b)

Each fix has a test that fails on the code before it.
- **The verifier (should-fix).**
  - It reads one message at a time and checks its time budget before each one.
  - The worker reads `verify` with the same 2-hour visibility timeout (`verify.VISIBILITY_S`) and batch 1.
  - A false `pgmq.archive` or `pgmq.delete` means another holder already finished the message. The transaction rolls back and nothing is re-sent (`queue.LostOwnership`). This holds for acks, retries, failures and dead letters, on every queue.
  - A pass holds a Postgres advisory lock, so executions never overlap. The job runs one replica, and `verifierParallelism` is gone.
  - Its audit rows say `system:verifier`.
  - `test_verifier_job.py`: 7 new tests, all failing before.
- **`test_enrol_spec.py`:** a field whose absence the code refuses with a 400 must be in the spec branch's `required`. With `request_id` dropped from both `required` lists, 3 of 3 new cases fail and the older tests pass.
- **The relay's egress** to the internal subnet allows only `apiTcpPort`, not 443 or 31443.
- **Caller ranges:** a range in `enrolAllowedCidrs`, `webAllowedCidrs` or `adminAllowedCidrs` that covers `caeRelaySubnetPrefix` fails the deployment (Bicep `fail()`), naming the range. That includes `0.0.0.0/0` for web and enrol. `test_deploy_env.py` evaluates the NSG rule and the allow-lists: 19 new cases fail on the previous template.
- **IPv4-mapped addresses:** IPv4-mapped IPv6 ranges and peers (`::ffff:10.60.0.0/122`) compare as IPv4 in the forwarded-mode overlap check, in `peer_in` and in the X-Forwarded-For peer check. An IPv6 range that holds every mapped address is refused. `test_t5.py`: 3 new tests, all failing before.

Lanes at the round's last commit:
- service `uv run pytest -q` on its own Postgres 17 (port 55454): 1485 passed, 50 skipped (after merging origin/main at a6f63db);
- relay `uv run pytest -q`: 127 passed;
- service `ruff check` and `ruff format --check`: clean;
- `deploy/azure/check.sh`: 29 ok;
- `deploy/azure/test-whatif.sh`: 16 ok;
- `deploy/teams/test-build.sh`: 11 ok;
- `deploy/entra/test-register.sh`: 17 ok.

No Rust changed this round, so the edge lanes were not run again.

Follow-up (L, other owner): the verifier connects as the `worker` database role. Its own least-privilege role (read `catalog.location` and `catalog.file`, update the verify columns, insert audit rows, use the `verify` queue) belongs in `catalog/privileges.py`, which PR #35 also changes. It is left out here so the two merge cleanly.

## Tools

- `@microsoft/app-manifest` 1.1.3 (npm, MIT): fetched by `deploy/teams/build.sh` with `npm pack`, checked against its pinned sha512, for `MicrosoftTeams.schema.json` v1.17 only. Not installed into the repo. WP6: a build-time input for `LICENSES.md`.
- `moby/buildkit:buildx-stable-1`: pulled by the throwaway buildx builders, removed afterwards.
- No other new tool or dependency.

## Left

Classes: L local, P needs an account, O owner.

- **P** Everything only a deployment can confirm, now in `deploy/azure/README.md`'s VERIFY table: the TCP ingress on 8443 reaches `api` from the edge networks and the relay environment; ingress IP restrictions on a TCP ingress; `<name>.<default domain>` as api's FQDN; a `UserAssignedMSI` bot from Container Apps (and across tenants); a real what-if and deployment through `whatif.sh` (WP5).
- **P** `register.sh` on a real tenant: the groups claim as on-premises SIDs (no manifest switch found; a manual step), publishing to `appCatalogs` with the admin's az token, the Exchange RBAC steps for `--mail-sender`. The Teams manifest's `isNotificationOnly: false` (VERIFY whether true still delivers card invokes).
- **P** Pushing the images to the client's registry and confirming the digests survive the push.
- **O** The registry. `api`'s own environment, if forwarded mode is ever wanted back.
- **L, other owners** (outside WP1's globs; see the report):
  - `docs/milestones/real-azure-checks.md` "Direct replica access" (WP5) should test the TCP path.
