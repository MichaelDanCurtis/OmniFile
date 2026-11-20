# Portal and partner API, v1

**Status: M4, built (2026-10-06).** The YAML's main response schemas carry `examples` (each validated against its schema by the contract test), for the SPA's mocks. The `web` app (`service/src/omnifile_service/web/`) implements every route in §10. The machine-readable form is `service/openapi/portal-api.yaml` (OpenAPI 3.1). When the two disagree, fix both. A test (`service/tests/test_portal_api_contract.py`) checks the endpoint index (§10) against the YAML. The front end generates its client from the YAML.

Served by the `web` container app (`ARCHITECTURE.md` §1): private ingress, no client certificates, catalog access only. Browsers use it through a session; API clients (registered applications, such as a migration tool) use it with Entra app tokens or OmniFile API keys. It is one JSON API for both. M4 builds the portal routes and the partner proposal route. M5 adds the rest of the partner API (§11) on the same base path and auth.

## 1. Shape

- **Base URL:** `https://<web host>`. JSON under `/v1`; sign-in under `/auth`; the SPA's files at `/`. Any other `GET` without a file extension returns the SPA's `index.html`, so client-side routes work.
- **Bodies:** JSON (`application/json`), UTF-8, at most 1 MiB. Errors are `application/problem+json` (§7).
- **Identifiers** (`file_id`, `version_id`, `share_id`, `dataset_id`, `recall_id`, `pin_id`, `hold_id`, …) are 64-bit integers as **decimal strings**, as in the control API.
- **Times** are RFC 3339 UTC. File times are also given as `mtime_ns` (decimal string of nanoseconds). Dates without time (`archive_at`) are `YYYY-MM-DD`.
- **Money** is `{"amount": "12.34", "currency": "USD"}`, a decimal string. A range adds `"high": "45.60"`, with `amount` the low end.
- **Names** are returned as stored: UTF-8, bytewise order. Clients must escape them for display.
- **Versioning:** `/v1` changes are additive (new fields, routes, codes and enum values). Clients ignore unknown fields.
  - **Open enums.** A response field marked `x-extensible-enum` in the YAML (instead of `enum`) may gain values without notice: new backend kinds (an on-premises NAS, say), storage layouts, logical tiers, restore priorities, cost items, file states and migration states (`handed_off` is reserved, decision 35). Clients must render an unknown value, showing the raw value, and never fail on one. The open fields today: `LogicalTier`, `TierState.restore_state`, `Entry.state`, the `priority` of `RecallOption`, `Recall` and `RecallCost`, `TimelineSegment.account`, `CostItem.item`, `PriceTable.backend_kind`, and `Dataset.backend_kind`, `storage_layout` and `migration_state`; for partners, `PartnerEvent.kind` and `entity`, `Location.state` and `mode`, `Migration.state`, `mode`, `cutover` and `copier_kind`, the tombstone's `deleted_by.kind` and `proof`, and `RecallBatch.state` and its items' `status`.
  - **Request enums stay closed** (for example `PinTier`, the `backend_kind` path parameter): a new value there is added to the YAML when the server accepts it. `RecallRequest.priority` is the exception: it takes any string, and the server accepts only a priority that the file's `recall-options` list as offered (§5.5), so a new priority is additive like the response side.

## 2. Authentication

### 2.1 Browsers: backend-for-frontend sessions

The browser never sees an Entra token. `web` is a confidential OIDC client.

1. `GET /auth/login?return_to=<path>` (rate-limited per client address, `429 throttled`: each call stores a pending sign-in)
   - `return_to` must be a same-origin path beginning with `/`. Anything else becomes `/`.
   - `web` stores an `oidc_login` row (`state`, `nonce`, PKCE `code_verifier`, `return_to`, 10-minute expiry) and sets `__Host-omnifile_login=<state>`, `HttpOnly; Secure; SameSite=Lax; Path=/`.
   - It redirects (302) to the tenant's authorize endpoint: `response_type=code`, `code_challenge_method=S256`, `scope=openid profile`, `response_mode=query`. Add `prompt=login&max_age=0` when re-authenticating (§2.4).
2. `GET /auth/callback?code&state`
   - `state` must equal the login cookie and an unexpired `oidc_login` row. The row is consumed once.
   - `web` redeems the code with the PKCE verifier. It authenticates as a client with a **client assertion from its managed identity** (a federated credential on the app registration); dev mode may use a static secret instead.
   - It validates the ID token: signature by the tenant JWKS, `iss`, `aud` = client id, `tid` = our tenant, `nonce`, `exp`, `iat`.
   - It reads `oid`, `name`, `preferred_username`, `roles`, `onprem_sid`, `groups` (on-premises group SIDs), `auth_time`, and the overage markers `_claim_names` / `hasgroups`.
   - It upserts the identity (§3.4), creates a `web_session`, clears the login cookie, sets the session cookie, and redirects (302) to `return_to`.
3. **Session cookie:** `__Host-omnifile_session=<256-bit random>`, `HttpOnly; Secure; SameSite=Strict; Path=/`. The catalog stores only its SHA-256.
   - `Strict` is safe for an SPA: the first navigation from a Teams link has no cookie, but `index.html` needs none, and every API call after it is same-site.
   - **Idle timeout 60 minutes, absolute 10 hours.** After either, `401 unauthenticated`, and the SPA starts `/auth/login` again (silent if Entra still has a session).
4. `GET /auth/session` → `200 Session` (user, roles, `csrf_token`, expiries), or `401 unauthenticated`. The SPA calls it on load.
5. `POST /auth/logout` (CSRF-checked) deletes the session, clears the cookie, and returns `200 {"logout_url": "<Entra end-session URL>"}`.
6. **Session fixation:** every sign-in issues a new session id, and any session cookie the browser brought to the callback is deleted.
7. A re-authentication sign-in (`reauth=1`) is refused unless the ID token's `auth_time` is no older than the start of that sign-in.

**CSRF.** Every `POST`, `PUT`, `PATCH` and `DELETE` from a session (`/auth/logout` included) needs both:
- header `X-CSRF-Token` equal to the session's token, compared in constant time;
- an `Origin` header equal to the portal's origin.

Either missing or wrong gives `403 csrf_failed`. Bearer-token callers are exempt; they send no cookies.

**Response headers on every response:**
- `Content-Security-Policy: default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'`
- `X-Content-Type-Options: nosniff`
- `Referrer-Policy: no-referrer`
- `Strict-Transport-Security: max-age=31536000`
- `Cache-Control: no-store` on `/v1` and `/auth`. Hashed assets are `public, max-age=31536000, immutable`.

### 2.2 Partners: Entra app tokens

- `Authorization: Bearer <access token>`, from client credentials or workload identity federation (`ARCHITECTURE.md` §6). No secrets on our side.
- Validated against the tenant JWKS:
  - `iss` = `https://login.microsoftonline.com/<tid>/v2.0`;
  - `aud` = our API's application ID URI, or its client id;
  - `tid` = our tenant;
  - `idtyp` = `app`;
  - `exp` and `nbf`, with 5 minutes of leeway.
- App roles come from `roles`: `Policy.Propose`, `Recall.Request`, `Archive.Read`, and in M5 `Archive.Adopt`.
- The principal is `app:<azp>`. Delegated (user) tokens are refused with `401 unauthenticated`.
- Only the routes marked `B` in §10 take a bearer token; the others answer `401 unauthenticated` to one, so a token is never mixed with a session.
- `RS256` only; the key is chosen by `kid` from the tenant JWKS (cached, refetched for an unknown `kid` at most every 5 minutes).
- An app is never ACL-filtered: it acts at dataset level, and every call is audited (`access-control.md` §5).
- **Registered clients only** (§5.21). A valid token is accepted only if its application (`azp`) is registered in Settings, API clients and enabled: otherwise `403 client_not_registered` or `403 client_disabled`. Its roles are cut to the client's scopes there. Each client names its **owner**, a person in the tenant; audit rows of its calls carry `details.api_client` (`api_client_id`, `owner_identity_id`), and proposals record both.

### 2.2.1 OmniFile API keys

Entra app credentials are the recommended way in: nothing is stored or shared on our side. For a client that can't hold one, an ArchiveAdmin can issue an **OmniFile API key** for a registered client (owner decision, 2026-11-04; §5.21).

- **Form:** `ofk1.<key_id>.<secret>`: `key_id` is 16 hex characters, public; `secret` is 32 random bytes, base64url. The catalog keeps only the secret's SHA-256 (`catalog.api_key`), compared in constant time. The key is in the creating response only, and those routes are never replayed from the idempotency store.
- **Sent only as** `Authorization: Bearer ofk1.…`, on the routes marked `B`. A key in a query string is refused (`400 invalid_request`), and every log line, uvicorn's access log included, has keys redacted. The audit records only `key_id`.
- **Scopes** within the client's. The caller acts as its client (`app:<azp>`), with the client's owner, so the owner rule of §5.21 applies to key calls too.
- **Expiry:** 90 days by default, at most 365. **Rotation** makes a new key and shortens the old one's life to an overlap (7 days by default). **Revoke** works at once. A disabled or revoked client's keys stop working with it.
- **Refusals** are `401 unauthenticated` ("API key rejected"), audited as `api_key.refused` with `key_id` and the reason (`unknown`, `wrong_secret`, `expired`, `revoked`, `client_disabled`, `malformed`): one row per address, reason and key per minute (unknown and malformed keys count as one), with `count`, the refusals since the previous row. An address that keeps sending refused keys (20, then one a second) gets `429 throttled` for each further refused key, with no audit row; a malformed key costs no lookup, and a valid key still gets in.
- **Rate limits** are per key (`key:<key_id>`), as idempotency is.
- Each key shows when it was last used and from which address.

### 2.3 Dev issuer

With `OMNIFILE_DEV_MODE=1` only, `web` mounts `/dev-idp/`:
- an OpenID configuration and JWKS (a generated RSA key);
- an authorize page that lists seeded users (oid, name, `onprem_sid`, group SIDs, roles), or takes them as query parameters for tests;
- a token endpoint that issues ID tokens, and app tokens for `client_credentials` with requested roles.

`web` refuses to start with the dev issuer configured unless dev mode is on.

### 2.4 Re-authentication

- WORM enable and cancel (§5.6) and legal holds need `auth_time` within the last **10 minutes**. Otherwise they return `401 reauth_required` with `max_age_s: 600`.
- `auth_time` comes only from the ID token's `auth_time` claim, which Entra emits only on request: `/auth/login` asks for it (`claims={"id_token":{"auth_time":{"essential":true}}}`), and the app registration should add it as an optional claim. Without it a session never counts as recently authenticated (`iat` is fresh after a silent SSO too), and a `reauth=1` sign-in is refused.
- The SPA then sends the user to `/auth/login?return_to=…&reauth=1`, which forces `prompt=login`.

## 3. Authorisation

### 3.1 Roles

| Role | From | Grants |
|---|---|---|
| `ArchiveAdmin` | Entra app role, assigned to an AD group or to named people (decision 89) | Policies, proposals approval, share settings, WORM, legal holds, price table, audit, the group directory. **Not** file content access |
| `DataSteward` | Entra app role | Tier pins and keep-online holds on items they can read; proposals; read policies |
| `User` | Entra app role; with `OMNIFILE_WEB_ROLE_HOLDERS=people`, also every signed-in user whose token carries an AD SID, with no assignment (decision 89) | Browse, recall, rewind and download what their AD groups can read |
| Share **Owner**, **Writer**, **Reader** | Membership of the share's AD groups (`share.owners_group_sid`, …), by SID | Owner: rewind depth, open policy, Writers and Readers groups, pins and holds in that share. Owner or Writer: restore a version |

A signed-in user with no app role gets `403 role_required` on every `/v1` route except `/v1/me`.

### 3.2 Reading a file

A user may see or act on a file or folder only if `catalog.acl_can_read(acl.read_sids, acl.deny_sids, <user SID>, <group SIDs>)` is true for its `acl_id`.

- **Listings** filter children by their own ACL.
- **A folder must be readable** to list it.
- **A file's ancestors** must be readable for its path to be shown in full. Unreadable ancestors appear as `{"file_id": null, "name": null}`.
- **Old versions** (rewind) need both the version's ACL at that time (`file_version.acl_id`) and the current ACL of the folder being browsed (`tiers-versioning-worm.md`, decision 30).
- **Unreadable items are `404 not_found`,** never 403, so their existence isn't revealed. A readable item on which the role or share role is missing is `403 forbidden`.
- **Roles never grant read.** ArchiveAdmin sees totals without names (owner to confirm; `M4.md`). The partner reads that name files (§5.11) are for apps only, for that reason (M7 review).

### 3.3 Group SIDs

The session's group list is the token's `groups` (on-premises SIDs), plus `<domain SID of onprem_sid>-513` (Domain Users) and `S-1-5-11` (Authenticated Users).

- **Overage** (`_claim_names.groups` present, or `hasgroups`): `web` uses `identity.group_sids` if it is under 24 h old, and sends `{"identity_id"}` on `identity_resolve`.
- With no usable groups, ACL-checked routes return `503 groups_pending` (retryable, `retry_after_s: 5`) until the worker has resolved them.
- A user with no `onprem_sid` (cloud-only) has no AD identity: every ACL check is false. Role-only routes that read still work; routes that record a person (saving or approving a policy, pins, holds, restores, downloads, price tables) answer `403 forbidden`, because every catalog row names an AD identity.

### 3.4 Identity rows

- On sign-in, `web` finds `catalog.identity` by `sid = onprem_sid`, or else by `entra_object_id = oid`. It creates the row if neither exists.
- It sets `entra_object_id`, `upn`, `display_name`, and `group_sids` with `groups_source = 'token'` and `groups_resolved_at`.
- Audit rows use actor `user:<identity_id>`, `actor_sid`, and `proof = 'proven'`.

## 4. Conventions

### 4.1 Pagination

- `?cursor=<opaque>&limit=<n>`. `limit` is 1–500, default 100; folder listings allow up to 1,000.
- Responses are `{"items": [...], "next_cursor": "<opaque>" | null}`. `null` means the end.
- Cursors are valid for 1 hour, then `410 cursor_expired`.
- Order is stated per route (folders: by name, bytewise).

### 4.2 Idempotency

- Every `POST` under `/v1` needs `Idempotency-Key` (16–128 characters; the SPA uses UUIDv7). The exceptions are `POST /v1/policies/preview`, `/v1/pins/preview` and `/v1/shares/{id}/settings/preview`, which change nothing.
- Same rules as `control-api.md` §8: the principal `user:<oid>` or `app:<client id>`, 24 hours, same key and same body replays with `Idempotent-Replayed: true`, a different body gives `422 idempotency_key_reused`, and a request still running gives `409 request_in_progress`.

### 4.3 Concurrency

Shares and price tables carry `etag` (the row's `entity_version`, quoted).
- `PATCH /v1/shares/{id}/settings`, `PUT /v1/shares/{id}/access`, the WORM routes and `PUT /v1/price-tables/{kind}` need `If-Match: "<etag>"`.
- A stale or missing `If-Match` gives `412 precondition_failed`, or `428 precondition_required` when it is absent.
- Policies use `base_version` in the body instead (§5.7).

### 4.4 Limits

- 20 requests/s per principal, burst 40. Previews 5/s.
- Over the limit: `429 throttled` with `Retry-After`.
- Server time budget: 5 s; previews 1 s.

## 5. Resources

### 5.1 Me and the group directory

- `GET /v1/me` → `Me`: object id, catalog `identity_id`, name, UPN, SID, roles, group source (`pending` while an overage is resolved) and age, and the share ids where the user is Owner, Writer or Reader.
- Every `PersonRef` (who set a pin or hold, proposed, approved or decided) carries `identity_id` for identities, so the SPA can recognise the caller's own rows by comparing with `Me.identity_id`. The server still decides what the caller may do.
- `GET /v1/directory/groups?q=&cursor&limit` → AD groups known to the catalog (`identity` rows of kind `group`, named by the worker's nightly Graph sync).
  - `q` matches display name or sAMAccountName (case-insensitive prefix, wildcards taken literally), or an exact SID.
  - Callers: ArchiveAdmin, or an Owner of any share.

### 5.2 Shares and the access screen

- `GET /v1/shares` lists shares. ArchiveAdmin and DataSteward see every share. Others see the shares where they are Owner, Writer or Reader, or can read the root.
- `GET /v1/shares/{share_id}` → `Share`. It includes:
  - trust level (`proven` or `host_trusted`) and the host-trust override;
  - groups, open policy, rewind depth and maximum, and WORM state with `lock_deadline`;
  - the last full ACL scan time;
  - `my_access`;
  - `checks` from `share_checks()`, including the Kerberos throughput note.
- `PUT /v1/shares/{share_id}/access` sets `owners_group_sid` (ArchiveAdmin only), and `writers_group_sid` and `readers_group_sid` (ArchiveAdmin or the share's Owners).
  - The change reaches the edge through `share.upsert` (`control-api.md` §5), and the edge applies it as the root and default ACL (M2).
  - Audited as `share.access`.

### 5.3 Browse

- `GET /v1/dirs/{dir_id}/children?as_of=&cursor&limit`: a folder's readable children, by name.
  - **Without `as_of`:** live rows (`Entry`): tier, state, archive date, effective pin, holds (with each active hold's id, kind, until and the folder it is inherited from), active recall.
  - **With `as_of`:** rows as of that instant (`AsOfEntry`, from `catalog.folder_as_of`), each with the version valid then and that version's location tier.
  - `as_of` older than the share's `rewind_depth_days` gives `410 beyond_rewind_depth`. A future time gives `400`.
- `GET /v1/files/{file_id}` → `FileDetail`: the `Entry` fields plus path, hashes, owner and mode, clocks, retention and legal hold, version count, and the share's depth.

### 5.4 Versions, restore and download (decision 30)

- `GET /v1/files/{file_id}/versions?cursor&limit`: versions inside the rewind depth (or retained by WORM), newest first. Each has kind, commit time, size, tier, archived, retention and hold.
- **Every version route** (restore, download, recall options and `POST /v1/recalls` with a `version_id`) sees only the versions this listing shows: read on the version's own ACL at that time and on the file now, and inside the rewind depth or retained by WORM. Anything else is `404`, so guessed version ids reveal nothing.
- `POST /v1/files/{file_id}/versions/{version_id}/restore` `{target}` (Owner or Writer of the share, and read on that version) → `202 VersionRestore`. A share shows only current versions; older ones live as stored blob versions (decision 34).
  - **`target = overwrite`**: the old bytes become a new current version. The previous current stays as an older version. Allowed on WORM shares, because it is a new version. On a deleted file: `409 version_not_restorable`.
  - **`target = restored`**: a new file next to it, `<stem>-restored<ext>`.
  - **`target = restored_timestamped`**: `<stem>-restored-<YYYY-MM-DD-HHMMSS>Z<ext>`, UTC, no colons (SMB-safe), from the request time.
  - The extension starts at the last dot; a name whose only dot leads it (`.bashrc`) has none. The stem is shortened to keep the name within 255 bytes.
  - A restored name that is already taken in the folder is `409 name_taken`, at once; nothing is ever overwritten. The worker checks again when it commits, and fails the restore (`failure` starts `name_taken`) if the name appeared meanwhile.
  - An archived version is recalled first (`state = waiting_recall`, with `recall_id`). If the version became archived after the request, the worker starts or joins the recall itself.
  - Tombstones and pruned versions give `409 version_not_restorable` (or `404`, when the tombstone has no ACL to read).
  - How the worker does it: it copies the bytes server side to the restore's own blob (`<prefix>restores/<restore_id>`), records that copy (`state = copying`), and only then writes the new version, its location and the file (`origin = 'service'`) in one transaction, with a CRC read-back on `verify`. A throttled or interrupted restore resumes from `copying` without copying again, and never leaves a half-made file. The file's edge state becomes `stub` until the edge fetches the new bytes.
  - A restore fails (`state = failed`) if its folder is gone, or the version's recall failed.
  - Audited as `version.restore`, with the target and the resulting name.
- `GET /v1/version-restores/{restore_id}` → `VersionRestore` (the requester, or ArchiveAdmin).
- `POST /v1/files/{file_id}/versions/{version_id}/downloads` → `202 Download`.
  - The worker mints a 10-minute read SAS for that blob version on the private endpoint (or the live restored copy of an archived one). VERIFY: `rscd` (the disposition and file name) is not set yet; `StorageBackend.edge_credential` has no parameter for it.
  - Read on the version means its own ACL at that time and the file's now (as for rewind).
  - An archived version gives `state = needs_recall` with `recall_options`.
  - Audited as `version.download`.
- `GET /v1/downloads/{download_id}` → `Download` (the requester only). The SPA polls every second for up to 30 s, then follows `url`. The URL is never logged, and it is cleared from the catalog when it expires.

### 5.5 Recalls

- `GET /v1/files/{file_id}/recall-options?version_id=` → `RecallOptions`: whether a restore is needed, and per priority whether it is offered, its ETA and confidence, and a fee estimate.
  - Built from `policy.recall.plan_recall` over `capabilities()`, with the account's High backlog.
  - **High is not offered where it can't be faster** (anti-pattern 5).
  - **No ETA above the no-promise size** (10 GB on Azure). `eta_confidence` is `provider_sla`, `measured` or `none`, as in the control API.
- `POST /v1/recalls` `{file_id, version_id?, priority, due_by?}` → `201 Recall` when new, `200 Recall` with `joined: true` when joining.
  - `priority` must be one the file's `recall-options` list with `offered: true` (today `standard`, or `high` where it is faster). Anything else, including a listed option that isn't offered, is `400 invalid_request` with `field: priority`.
  - Session callers need read on the file. Apps need `Recall.Request`.
  - It starts or joins the one active recall of that content version (`recall_one_active`). The requester is `via = portal` or `api`, `proof = proven`.
  - It sends `restores` (new) and `notify` in the same transaction.
  - `version_id` defaults to the current content version.
  - A file that is online is `409 recall_not_needed`, with the file's state.
- `GET /v1/recalls?scope=mine|share|all&share_id=&state=&cursor&limit`:
  - `mine`: any user;
  - `share`: Owners of that share, or ArchiveAdmin;
  - `all`: ArchiveAdmin.
  - Newest first.
- `GET /v1/recalls/{recall_id}` → `Recall`, the **four-step view** (with the file's `parent_id`, `name` and `share_id` when the caller can read the file):
  - `steps` = Requested, Rehydrating (including `copying`), Downloading, Available, each `done`, `current`, `pending` or `failed`, with `at` from `recall_state_log`;
  - ETA and confidence, priority, the requester count, and whether the caller is one.
  - **`superseded`** (state `superseded`, control API §4.4): the file was replaced or deleted while it was being recalled, so the recall stopped. `superseded` is `{reason, text, by}`: `reason` is `replaced` or `deleted`, `text` says it in plain words, and `by` is who did it, as a display name. `by` is null unless the caller can read the file and the edge proved who it was. `failure` stays null, the steps the recall reached stay `done`, and none is `failed`. The page shows `text`, not a failure, and offers no retry: a replaced file's bytes are new.
  - Callers: a requester, or anyone who can read the file.
- M4 has no cancel route.

### 5.6 Pins, holds, WORM and depth (decisions 29–31)

- **Pin preview:** `POST /v1/pins/preview` `{items, tier, until?, override_archive?}` → per item: `checks` and fee from `pin_checks()` (early-deletion and minimum-duration fees, "this leaves Smart permanently", recall time, monthly cost change), plus totals. Nothing changes.
- **Pins:** `POST /v1/pins` `{items, tier, until?, reason, override_archive?}`.
  - Callers: share Owners or DataStewards, with read on every item. At most 1,000 items, files or folders.
  - Items that can't be pinned are listed in `skipped` with a code (`not_found` for an item the caller can't read or that doesn't exist, `forbidden` without the role, `override_archive_owner_only`); the rest are pinned. The same `skipped` rules apply to `POST /v1/holds`.
  - A pin replaces the item's active pin (`tier_pin_active`).
  - `override_archive` is Owners only.
  - The scheduler picks pins up (M3).
- `GET /v1/pins?share_id=&cursor`; `POST /v1/pins/{pin_id}/release`.
- **Holds:** `POST /v1/holds` `{items, kind, until?, reason}`.
  - `keep_online`: Owners or DataStewards, files or folders.
  - `legal`: ArchiveAdmin, with re-auth, wrapping `retention.worm.set_legal_hold`. The admin must be able to read the item (roles never grant read; owner to confirm, see `M4.md`).
  - Teams' **Keep online** button (decision 33, `teams.md`) sets the same `keep_online` hold, 14 days, for any reader of the file. It shows in these listings, and its setter may release it here.
  - `GET /v1/holds?share_id=&kind=&file_id=` (`file_id`: the holds that apply to it, on it or on a folder above it; a hold on a folder above carries `inherited_from` = that folder's id, as in `Entry.holds`); `POST /v1/holds/{hold_id}/release` (legal: ArchiveAdmin; keep_online: Owners, DataStewards or the setter).
- **Share settings:** `PATCH /v1/shares/{share_id}/settings`, with `If-Match`. Each field has its own role:
  - `on_open` and `wait_threshold_minutes`: Owners or ArchiveAdmin;
  - `rewind_depth_days`: Owners or ArchiveAdmin (`retention.depth.set_rewind_depth`);
  - `rewind_depth_max_days`: ArchiveAdmin (`set_rewind_max`);
  - `quiet_s`: Owners or ArchiveAdmin (decision 67). How long a file must sit untouched before it counts as finished: 10 to 600 seconds, or null for the share's default, which the share reports as `quiet_default_s` (60 on a WORM share, 10 on any other). It applies to every write on the share. It reaches the edge in `share.upsert` and takes effect there within seconds, with no restart. Audited in `share.settings` with `details.quiet_s = {old, new}`, where null is the default. Setting a WORM share below 60 s also needs `quiet_s_confirmed: true`, else `400 invalid_request` with `quiet_s_confirmed` as the field; the audit then adds `confirmed_below_worm_default: true`. The share reports `quiet_default_s` as its edge last sent it in the heartbeat, and `quiet_supported` says whether the edge has sent it at all (false for an edge that predates decision 67; it doesn't say the edge has this share's latest value), with `edge_name`;
  - `nfs_sec`, `nfs_clients`, `root_squash` and `host_trust_override {enabled, reason}`: ArchiveAdmin.

  The catalog's constraints apply. For example, `sys` needs named clients and `root_squash`: a violation gives `400 invalid_request` with `errors` (the constraint as `field`). A field the caller's role doesn't cover is `403 forbidden`. Changing `on_open` away from `auto` clears `wait_threshold_minutes`.
- `POST /v1/shares/{share_id}/settings/preview` returns the `checks` for proposed settings (the Kerberos note, host-trusted warnings), with nothing saved.
- **WORM** (ArchiveAdmin, re-auth, `If-Match`):
  - `POST …/worm/enable` `{retention_days, confirm_name, window_hours}`. `window_hours` is 1–24, default 24. A share whose own `quiet_s` is below 60 s also needs `quiet_s_confirmed: true` (decision 67), else `409 worm_violation`; `share.worm.confirm` then records `quiet_confirmed_below_worm_default`. `confirm_name` must equal the share name exactly, else `422 confirmation_mismatch`. Returns the share with `worm.lock_deadline`. Wraps `retention.worm.enable_worm`.
  - `POST …/worm/cancel` `{confirm_name}`: inside the window only, else `409 worm_window_closed`. The window ends at `lock_deadline` even if the auto-lock sweep hasn't run yet. Wraps `cancel_worm_test`.
  - `POST …/worm/extend` `{retention_days}`: once locked, only upwards, else `409 worm_violation`. Wraps `extend_worm_retention`.
  - **A WORM share can't use Smart.** Enable is refused with `409 layout_conflict` while the dataset's active policy is Smart.

### 5.7 Policies, preview and proposals

- **Policies are per dataset.** All shares of a dataset follow it.
- **The open policy is per share** ("what users see"). The editor saves it through §5.6, and previews it through `share_overrides`.
- `GET /v1/datasets` (ArchiveAdmin, DataSteward): datasets with backend kind, layout, shares, the active policy's summary, and the count of pending proposals.
- `GET /v1/datasets/{dataset_id}/policy` → the active `PolicyVersion`, or `404` if none.
- `GET /v1/datasets/{dataset_id}/policies?cursor`: history, newest first. Readable by ArchiveAdmin, DataSteward, Owners of a share on the dataset, and apps with `Archive.Read`.
- `POST /v1/datasets/{dataset_id}/policies` `{body, base_version, note?}` (ArchiveAdmin) → `201 PolicyVersion`.
  - `base_version` must equal the active version (`null` if none), else `409 version_conflict`.
  - A body whose preview has an `error` check gives `422 policy_invalid`, with `checks`.
  - Catalog layout errors (OF004) give `409 layout_conflict`.
  - On success, in one transaction:
    - the old version is `superseded`, the new one `active`;
    - `approved_by_identity_id` is the caller;
    - pending proposals based on the old version are marked stale (shown, not changed);
    - `policy.scheduler.reschedule_dataset` runs.
  - Audited as `policy.create`.
- `POST /v1/policies/preview` `{dataset_id, body, share_overrides?, assumptions?}` → `PolicyPreview` (ArchiveAdmin, DataSteward, apps with `Policy.Propose`).
  - Timeline, first-year cost per TB against keeping it hot, recall cost per TB, checks, and the price table used.
  - Pure: nothing is written. The SPA calls it 250 ms after the last edit.
- **Proposals** (anti-pattern 7: nothing applies itself):
  - `POST /v1/policies/proposals` `{dataset_id, body, rationale, base_version?, share_overrides?}` → `201 Proposal`. `share_overrides` carries the editor's "what users see" answers (`on_open`, `wait_threshold_minutes` per share of the dataset); they are stored with the proposal, shown in its preview, and applied to the shares only when an ArchiveAdmin approves it. Callers: API clients with `Policy.Propose` (a migration tool or any AI), ArchiveAdmin, DataSteward. The body is stored as given; it is checked, not applied. A body with error checks is still stored, and shown with its checks.
  - `GET /v1/policies/proposals?dataset_id&state&cursor`; `GET /v1/policies/proposals/{proposal_id}` (with `preview` and `diff` against the active policy). Apps see only their own.
  - `POST /v1/policies/proposals/{proposal_id}/approve` `{note?, accept_stale?}`: ArchiveAdmin, and **not the proposer** (`403 self_approval`; the catalog enforces it too).
    - It creates a `PolicyVersion` with `proposal_id`, as in `POST …/policies`, and applies the proposal's `share_overrides` to the shares (audited as `share.settings` with the `proposal_id`), in the same transaction.
    - A stale base needs `accept_stale: true`, else `409 version_conflict`.
    - Error checks give `422 policy_invalid`.
    - Approving is the only way a proposal's body becomes a policy.
  - `POST …/{proposal_id}/reject` `{note}` (ArchiveAdmin).
  - `POST …/{proposal_id}/withdraw` (the proposer).
  - A decided proposal gives `409 proposal_not_pending`.

**The policy body** (`catalog.policy.body`; `policy/model.py`, plus `notify`):

```json
{"mode": "scheduled", "clock": "last_read",
 "warm_day": 30, "cold_day": 120, "archive_day": 365,
 "stub_at": "archive", "resident_mb": 4, "restub_after_hours": 24,
 "notify": {"requester": true, "owners": false, "watchers_group_sid": null, "email_fallback": true}}
```

- `mode = smart` takes no `warm_day` or `cold_day`.
- `archive_day` is required.
- Days strictly increase.
- A body without `archive_day` is an M1 policy (`stub_after_minutes`). The editor shows it read-only, with a "convert" action that fills in a scheduled policy.

### 5.8 Price tables

- `GET /v1/price-tables/{backend_kind}` (ArchiveAdmin, DataSteward) → the table the preview uses: the built-in, dated table (`policy/prices/<kind>.json`) or the admin's override. The built-in table's ETag is `"0"`; the first `PUT` sends `If-Match: "0"`.
- The table carries `as_of`, `region`, `currency`, `source`, and `verify` (true while any number is unchecked).
- `PUT /v1/price-tables/{backend_kind}` (ArchiveAdmin, `If-Match`) stores an override. `DELETE` returns to the built-in table.
- Audited as `price_table.set`.

### 5.9 Audit

- `GET /v1/audit?share_id&file_id&actor&action&from&to&cursor&limit`, newest first.
- ArchiveAdmin sees everything. A share's Owners see only rows with that `share_id`, which they must give, and of those, rows about a file only if they can read the file (roles never grant read).
- `client_addr` is shown to ArchiveAdmin only.
- The audit log is append-only (0003).

### 5.10 Sources (M5, partner)

Data that already lives in a backend, registered as a migration source of a dataset and seeded into its catalog (`catalog-seeding.md`). Apps: `Archive.Adopt` to register or retry, `Archive.Read` or `Archive.Adopt` to read. Signed-in users: ArchiveAdmin. Apps act at dataset level (§2.2).

- `POST /v1/sources` `{dataset_id, backend_id, container, prefix?, path?, inventory?, groups: {owners, writers?, readers?}}` → `201` the source, `registered`.
  - `backend_id` names a backend an ArchiveAdmin configured (the setup wizard); `container` is one of its buckets.
  - Or, instead of both, the draft's inline `backend: {kind: "s3", config: {bucket, region, account_id?, requester_pays?}}` (`POST /v1/sources` only). It **finds** the backend an ArchiveAdmin configured for that bucket (an S3 backend in that region whose `buckets` lists it, private networking first); it never creates one. Backends, and their private-network and credential settings, stay an admin's to create. `container` (A's name for the bucket) and `private_network: true` are accepted too, as no-ops.
  - Any other config field is `422 config_invalid`, naming it (`backend.config.endpoint_url: …`): endpoints, roles, web-identity audiences, credentials, CA bundles, queues, Batch settings and `private_network: false`. A partner who could set them could make the service send its token, or its traffic, to a host of their choosing (line B's review, 88c525b). So is `requester_pays: true` (not supported yet), a bucket no admin configured, and an `account_id` other than the configured backend's.
  - `prefix` is `''` or ends in `/`; prefixes of one bucket may not overlap. `path` is the folder of the dataset the objects appear under (`''` = the root; missing folders are created).
  - `inventory` says where seeding reads the objects (`catalog-seeding.md` §7), in order of preference:
    - `{kind: "s3_metadata", table_bucket_arn, namespace?, table?, journal_table?}`: the bucket's S3 Metadata tables (the live inventory table, then the journal keeps the source current). The ARN is the account's `aws-s3` table bucket, in the backend's region; the endpoint and credentials are the backend's, never the request's.
    - `{kind: "s3_inventory", bucket, manifest_key}`: the manifest of a CSV S3 Inventory.
    - `{kind: "listing"}`: list the prefix, even where the bucket has Metadata tables.
    - Absent or null: the bucket's Metadata tables if its backend names them, else a listing. The source shows the inventory chosen.
  - `groups` are AD group SIDs; they become the ACL of every seeded folder and file (decision 28). Owners is required.
  - A backend other than the dataset's own sets the dataset `migrating` from it (decision 23).
  - Refusals: `400 invalid_request` (a bad field; an unknown dataset or backend, since the body names them, not the URL; a bucket not in the backend; an inventory on a non-S3 backend) and `409 source_conflict` (an overlapping prefix, a dataset already migrating from another backend, a path through a file).
  - `web` writes only the catalog. The worker reads the inventory's snapshot time and starts the change feed on the first seed step, so an inventory older than 14 days, or one whose gap can't be replayed, ends the source `failed` with `last_error`.
  - Audited as `source.register`.
- `POST /v1/adopt`, the same body → `201` a source of `kind: "adopt"`: data an API client already moved into the dataset's **own** backend, cataloged at its original paths (`prefix` → `path`, as above). Nothing migrates: the dataset's state is left as it is, and seeded rows have `origin: adopt`. Another backend is `400 invalid_request` (register a source instead). Audited as `source.adopt`. Sources and adoptions share the reads and retry below; each carries its `kind`.
- `GET /v1/sources?dataset_id&state&cursor&limit`, newest first, and `GET /v1/sources/{source_id}`: state (`registered → seeding → seeded`, or `failed`), `snapshot_at`, `counts` (`objects_seen`, `files_created`, `dirs_created`, `already_present`, `skipped`), `last_error`, and `active_migrations`: how many of its migrations aren't cancelled. The portal offers to start a migration where it is 0.
- `POST /v1/sources/{source_id}/retry` `{}` resumes a `failed` source where it stopped; another state is `409 source_conflict`. Audited as `source.retry`.
- `GET /v1/source-backends` (ArchiveAdmin, session only) → `{backends: [SourceBackend]}`: the backends data can migrate from (v1: S3), for the portal's New migration wizard. Each has `backend_id`, `name`, `kind`, `region`, `private_network`, `buckets` (the buckets it serves, without OmniFile's own staging and Batch buckets; `null` when it lists none, so any bucket) and `metadata_tables` (buckets whose S3 Metadata tables are configured, which seeding reads by default). Never endpoints, roles or credentials. The portal picks one; it never creates one.
- **The portal's New migration wizard** registers a source with `POST /v1/sources`, waits for it to be `seeded`, then starts the migration with `POST /v1/migrations` (§5.17), which refuses a source still seeding (`409 migration_conflict`). If the second call fails, or the admin leaves while the source is seeding, the source stays and the Migrations page offers to start its migration.

### 5.11 Partner reads (M5)

Apps: `Archive.Read`. Never ACL-filtered (§2.2): an app reads at dataset level. So `files`, `restores` and `events`, which name files, are for apps only: a signed-in user, ArchiveAdmin included, gets `403 forbidden` (a role never grants read, §3.2) and sees names through the browse routes. `usage` (totals, no names) is also open to a signed-in ArchiveAdmin.

- `GET /v1/files?dataset_id&path&cursor&limit`, or `GET /v1/files?file_id` → `{entry, items, next_cursor}`. `path` is dataset-relative from `/` (the root) and finds a live entry; `file_id` (as an event names it) finds an entry even after it was deleted (`deleted_at` set, `path` its last one). `entry` is the file or folder; for a live folder, `items` is a page of its live children by name (empty for a file). A missing entry is `404 not_found`. Each row: `path`, `kind`, `size`, `mtime`, the edge `state`, the logical `tier`, `stored` (backend, container, object key, provider version and class of the current bytes), `restore` (state and expiry), `archive_at` and `next_action`, `hashes` (BLAKE3, CRC64-NVME, and MD5 where the provider's ETag is one), `duplicates` (other live files with the same BLAKE3: a count and up to 10 ids) and active `holds` on the file or a folder above it.
- `GET /v1/restores?dataset_id&state&triggered_by&since&cursor&limit`, newest first: provider restores (user recalls, migration waves, API), with the object they restore, `mechanism`, `speed`, `state`, `migrate_on_complete` (decision 15), `eta`, `completed_at` and `expires_at` (an S3 restored copy's expiry). An API client can fold them into its restore plan.
- `GET /v1/events?after&dataset_id&types&limit` → `{items, next}`: the catalog's ordered feed, the same one the edge reads (`control-api.md` §5), without the edge's own entities (`acl`, `edge`) and conflicts, merged with the typed lifecycle events below. Pulled, so it works over private networking.
  - Catalog events are thin: `cursor`, `kind` (`<entity>.<op>`: `file.upsert`, `file.rename`, `file.delete`, `dataset.upsert`, `share.upsert`, `recall.upsert`), `dataset_id`, `entity`, `entity_id`, `entity_version` and `at`, with `data: null`. File events also carry `file: {path, kind, deleted}`, the file's current path (its last one if deleted), so every event resolves; `GET /v1/files?file_id=` reads its state.
  - **Lifecycle events** (migration 0029) are typed and carry their facts in `data` (`file` and `entity_version` null). Triggers write them in the transaction of the change, so every writer emits them:
    - `migration.started` (a migration was asked for, through `/v1/locations` or `/v1/migrations`), `migration.verified`, `migration.cut_over`, `migration.failed`, `migration.cancelled`: entity `migration`, data `{migration_id, prefix, from_backend_id, to_backend_id, mode, state}`. `migration.cut_over` adds `source_deletes_pending`, the rows of the cutover report (§5.18), so a partner knows when to fetch it without polling. `migration.handed_off` is reserved (decision 35).
    - `recall.state`: entity `recall`, data `{recall_id, file_id, version_id, state, eta}`, on every state a recall enters. A `superseded` event also carries `superseded_reason` (`replaced` or `deleted`): the file was replaced or deleted while it was being recalled, which is neither a recall still running nor a failure to retry.
    - `restore.state`: entity `restore`, data `{restore_id, file_id, state, triggered_by, migrate_on_complete, expires_at}`.
    - `proposal.decided`: entity `proposal`, data `{proposal_id, state, proposed_by_app, decided_at}` when a proposal leaves `pending`.
  - `kind` is an open enum (§1): ignore kinds you don't know. `types` (comma-separated kinds) keeps only those; an unknown one matches nothing.
  - Both kinds of event take their ids from one sequence and are read below the same horizon, so one cursor orders them and one retention floor (and `410`) covers both.
  - `after` is the previous page's `next` (never null), `0.0` (the default) for every event still kept, or `now` for the cursor of now with no events. Events are read below the oldest running transaction, so one that commits late is never skipped; `next` moves past it only once it is seen.
  - A cursor older than the feed's retention is `410 cursor_expired`: read the state again (`GET /v1/files`) and continue from `now`.
  - So is a cursor issued before the catalog was restored from a backup (its feed incarnation, `control-api.md` §5). Cursors issued after a restore have a third part.
  - Not the portal's signed, one-hour cursor (§4.1): a feed cursor lasts as long as the events it points past.
- `GET /v1/usage?dataset_id` → bytes and monthly storage cost by tier, share and dataset, from the latest daily snapshot (a worker sweep; `computed_at` is null before the first; 35 days are kept).
  - `datasets[].stored`: every physical object a committed location names, which is what the provider bills (older versions kept for rewind and recall copies included; a blob shared by whole-file dedupe counts once), by backend, role, logical tier and native class, each with its `monthly_cost`.
  - `shares[]`: the current bytes of the live files under each share (its deepest covering share), by tier.
  - Cost is storage only, per month, at the class's `gb_month` in the backend kind's price table (§5.8) for the account the tier lives in. A class the table doesn't price has `monthly_cost: null` and is named in `unpriced`; S3 has no built-in table yet. `datasets[].monthly_cost` sums the priced rows, one entry per currency. Operations, retrievals and early deletion depend on activity: the policy preview estimates them.

### 5.12 Locations (M5, partner)

Moving a prefix of a migrating dataset to the dataset's own backend: copy, verify every file, cut over (`migration-engine.md`). Apps: `Archive.Adopt` to request, `Archive.Read` or `Archive.Adopt` to read; signed-in users: ArchiveAdmin.

- `POST /v1/locations` `{dataset_id, prefix, to_backend_id, mode?, copy?}` → `202` the location, `pending`.
  - `prefix` is an S3 key prefix (`''` or ending in `/`) inside one of the dataset's seeded sources; prefixes of one source may not overlap. `to_backend_id` must be the dataset's own backend (v1 moves S3 to Azure, decision 23).
  - `mode` is `serve` (the default). `hand_off` is reserved (decision 35) and refused.
  - `copy: omnifile` (the default): the worker runs Storage Mover jobs (decision 20). `copy: external`: the API client copies to `target_root` + the S3 key, and the worker only verifies and cuts over.
  - Refusals: `400 invalid_request` (a bad field, a prefix outside every source, another backend, `hand_off`), `409 migration_conflict` (the dataset isn't migrating, the source is still seeding, an overlapping prefix).
  - Audited as `location.request`.
- `GET /v1/locations?dataset_id&state&cursor&limit` and `GET /v1/locations/{migration_id}`: `state` (`pending → copying → verifying → verified → cut_over`, or `failed`, or `cancelled` through §5.17), the latest Storage Mover `run` (job, run id, state, counters), `files` (`total`, `verified`, `failed`), `journal_checked_through`, `verified_at`, `cut_over_at`, `last_error`.
- A location is a migration: the same row, by the same id, is in `/v1/migrations` (§5.17) with its lifecycle actions. A location request starts an `auto` cutover.
- Nothing is flipped before every live file under the prefix has a copy whose CRC64-NVME, read in Azure, equals the S3 object's, and the bucket's events are applied past the latest copy's start (decision 24). A copy that fails its check is removed and copied again by the next run. The S3 objects stay, as secondary copies.

### 5.13 Activity (M6)

The recall and upload dashboards (`docs/design/observability.md` §3). All of it is computed from the catalog.

- `GET /v1/activity?days&dataset_id` (ArchiveAdmin; apps with `Archive.Read`) → totals over the last `days` (1 to 90, default 30) UTC days, ending with today. `from` and `to` are the window's bounds.
- `days`: one row per day, `{date, uploads: {files, bytes}, recalls: {started, available, failed}}`. Uploads are content versions an edge committed. A recall counts as available or failed on the day it ended.
- `uploads`: `{files, bytes}` over the window, plus two counts as of now:
  - `unverified`: committed primary copies still waiting for their read-back;
  - `landing`: files still landing or uploading on an edge.
- `recalls`: `{available, failed, bytes}` over the window, `open` now, and `time_to_available_s: {p50, p90}` (null without a completed recall).
- `dataset_id` narrows everything to one dataset.

**The live feed (WP18).** `GET /v1/activity/live?since&wait&limit&kind&edge_id&share_id&identity_id` (the same gate: ArchiveAdmin; apps with `Archive.Read`) → `LiveActivity`: what happened since `since`, newest first, with live numbers. The code is `web/live.py`.

- `events`: up to `limit` (1 to 200, default 100) of six kinds, each with a stable `id`: `upload` (a content version an edge committed), `recall` (each state a recall reached), `tier` (the tiering worker's audited moves and refusals), `heartbeat` (an edge's latest heartbeat), `migration` (started, verified, cut over, failed, cancelled) and `annex` (an annex event: arrived, copied, verified, left...). `kind` (comma-separated), `edge_id`, `share_id` and `identity_id` (a person) narrow them, inside each source before its limit, so a busy edge can't crowd a quiet share's events out. A share holds the files under its root that no share nested in it holds; an unknown `share_id` is `404`.
- **Names follow read access (decision 28).** An event's file `name`, `path` (from the share's root) and `file_id` are given only when the caller's AD groups may read the file, as in browse; otherwise all three are null and `hidden` is true. Roles never grant read, so an ArchiveAdmin who can't read a folder sees that a file of the share changed, not which. An app has no SID and sees no names, and no `person`; it may not filter by `identity_id` either (`400 invalid_request`), which would say whose events they are. An app also sees only the datasets its API client's owner may see (§5.21's rule, as for the annex reads): its events, series and the edge strip's counts leave the others out. A migration's prefix is never sent. People are named by their catalog display name, as in the audit log.
- `counters`: uploads in the current minute and the last 15, bytes moved in the last 15 minutes (uploaded plus recalled), recalls in flight, and edges online (enrolled and heard from in 15 minutes) of those enrolled. `series`: per-minute uploads and bytes moved over the last 30 minutes. `edges`: each edge's status, last heartbeat, uploads and bytes in 15 minutes, open recalls and outbox.
- `cursor` is the next call's `since`. Each source is read from `since` less 30 seconds, so a transaction that committed late still shows; the page drops ids it already has. `since` absent, or older than 15 minutes, reads the last 15 minutes.
- **Long-poll.** With `since`, `wait` (0 to 25 seconds) holds the request until something newer than `since`, and not after now, exists (0031's commit floor dates some versions ahead), probing every 2 seconds with one query of an EXISTS per source; no catalog connection is held between probes. A replica holds at most 32 waiting polls (`WebSettings.live_max_waiting`), and no more than twice its catalog pool (8 on the small profile's 4); past that it answers at once with `live.capped: true` and `next_poll_s: 10`. `next_poll_s` is never under 2. Every source is read by time through an index (migration 0043, built `CONCURRENTLY` outside a transaction, so edge commits go on during the build).

### 5.14 Edges (M6)

What each edge last reported in its heartbeat (`control-api.md` §4.7; decision 37, `docs/design/observability.md` §2).

- `GET /v1/edges` (ArchiveAdmin; apps with `Archive.Read`) → `{edges: [Edge]}`, every edge that isn't revoked, by name.
- An `Edge` has:
  - `edge_id`, `name`, `state` (`pending` or `active`);
  - `software_version`, `kernel`, `guard_state` (`armed`, `degraded` or `down`);
  - `last_heartbeat_at`, `certificate_not_after`;
  - `health`: the heartbeat's gauges as sent (`outbox_depth`, `fills_running`, `recalls_waiting`, `data_free_bytes`, `data_capacity_bytes`, `fsal_connections`, `feed_lag_events`, `recovery_used`).
- `status` is judged by the service, with the alert rules' thresholds:
  - `silent`: enrolled, but no heartbeat for 15 minutes, or none ever;
  - `attention`: the smb-guard isn't armed, less than 10% of the data volume is free, or the break-glass account was used and not sealed again;
  - `ok` otherwise, and `pending` before enrolment.
- `state`, `status` and `guard_state` are open enums.

### 5.15 Adding an edge (M6)

An edge enrols with a one-time bundle (`control-api.md` §2), which the admin pastes into the edge's setup wizard (`docs/design/setup-wizard.md`). These calls issue it. A bundle is a credential, so only a signed-in ArchiveAdmin can get one, never an app.

- `POST /v1/edges {name}` (ArchiveAdmin, session only) → `201 EnrolmentBundle`:
  - `{edge_id, name, bundle, expires_at}`: a new edge, `pending`, and its bundle (`ofe1.…`), usable once until `expires_at` (24 hours);
  - `name` is what the portal shows: 1 to 63 lowercase letters, digits or hyphens, starting with a letter;
  - the catalog keeps only the token's SHA-256; the bundle is in this response and nowhere else;
  - refusals: `400 invalid_request` (a bad name or another field), `409 name_taken` (an edge with that name exists), `503 unavailable` (the service has no enrolment settings, `OMNIFILE_ENROL_PUBLIC_URL` and `OMNIFILE_ENROL_SERVER_CA`);
  - audited as `edge.add`.
- `POST /v1/edges/{edge_id}/bundle` (ArchiveAdmin, session only) → `201 EnrolmentBundle`: a new bundle for an edge that hasn't enrolled, for a lost or expired one. The previous bundle stops working. `409 edge_enrolled` for an edge that is `active` or `revoked`. Audited as `edge.bundle`.
- `POST /v1/edges/{edge_id}/revoke` `{}` (ArchiveAdmin, session only, re-auth within 10 minutes) → `200 {edge_id, name, state: "revoked"}`: for a lost or stolen filer. Its certificates, current and previous, are refused on every control call (`edge_revoked`), it can't renew, and an unused bundle stops working. Its datasets, shares and files stay. One-way: a filer found again enrols as a new edge. Revoking a revoked edge answers the same. Audited as `edge.revoke`. A bundle is a credential, so neither bundle route is replayed from the idempotency store.
- `GET /v1/edges/appliance` (ArchiveAdmin, session only) → `EdgeAppliance`, for the Add edge wizard (WP18): `release` (the service's version as the release's `VERSION`; one version names every part, `docs/release/README.md`), and per platform (`hyperv`, `vmware`, `kvm`, `azure`) the install image `appliance/convert.sh` writes, `omnifile-edge_<release>.<vhdx|ova|qcow2|vhd>`. A release publishes only the update feed today, so the images come in the release folder's `appliance-files/` (`docs/guide` §1.7) and `url` is null; it is set only when the operator names where they are published (`OMNIFILE_APPLIANCE_IMAGES_URL`, a base URL). No AWS image is built. Also `enrolment_configured` (the enrolment settings above are set) and `code_ttl_s`.

### 5.16 Adding datasets and shares (M6)

A share decides who reaches the data, so only a signed-in ArchiveAdmin adds one, never an app. The edge renders each share into its Samba and Ganesha config from the `share.upsert` it receives, gives the share root the groups' ACL, and reloads both servers (`docs/design/share-provisioning.md`).

- `GET /v1/backends` (ArchiveAdmin, DataSteward) → `{backends: [Backend]}`: `{backend_id, name, kind, private_network, can_be_home}`. S3 is never a dataset's home (decision 18): it is a migration source (§5.10).
- `POST /v1/datasets {name, backend_id, edge_id, storage_layout?, key_prefix?}` (ArchiveAdmin, session only) → `201 Dataset`:
  - a dataset on that edge, with its root folder (owned by root, `0770`, until the edge gives it a share's groups);
  - `storage_layout` is `two_account` by default; a share that will be WORM needs `single_account` (decision 31);
  - `key_prefix`: where its blobs go in the backend, empty or segments ending in `/`;
  - refusals: `400 invalid_request` (a bad name, an S3 or unknown backend, an unknown or revoked edge, a bad prefix), `409 name_taken`;
  - audited as `dataset.add`.
- `POST /v1/shares NewShare` (ArchiveAdmin, session only) → `201 Share`:
  - a share of the dataset's root (`path` `/`, the default), or of an existing folder (`path` `/projects/2026`), on the dataset's edge. The share's name is its SMB share name and its NFS pseudo path (`/<name>`); for a root share it is also the root folder's name on the edge, so a folder can be shared only once the dataset's root is;
  - every rule the edge checks again before serving a share (`access-control.md` §3) is checked here first: at least one protocol; NFS needs a security flavour; `sys` needs a list of named hosts or subnets (no wildcard) and root squash; `nfs_sec` and `nfs_clients` only for NFS; group SIDs; `auto` with minutes; rewind depth within its maximum; a `sys` share over folders with a restricted group needs `host_trust_override`, which is audited;
  - the service gives the share its NFS export id (`nfs_export_id`), the next free one on the edge from 2. It never changes for the share;
  - the response carries `checks`, among them the Kerberos throughput note;
  - refusals: `400 invalid_request` naming the field, `409 dataset_shared` (that folder is already shared), `409 name_taken` (the edge has a share with that name, in any case);
  - audited as `share.add`.
- `DELETE /v1/shares/{share_id}` (ArchiveAdmin, session only, `If-Match`) → `200 {share_id, removed: true}`:
  - the share stops being served (`share.delete` reaches the edge). Only the share goes: the dataset, its files and their versions stay, and a new share can serve them again;
  - refusals: `409 worm_violation` (a WORM share is never removed), `409 share_in_use` (a root share while shares of the dataset's folders need its folder on the edge);
  - audited as `share.remove`.

### 5.17 Migrations (M5, partner)

The lifecycle of a migration prefix (`migration-engine.md`), over the same rows as §5.12; ported from line B (PR #10, 8344f8a, c876e30, 88c525b). `web` writes only the catalog and wakes the worker's `migrate` queue. Reads: apps with `Archive.Read` or `Archive.Adopt`; signed-in ArchiveAdmin.

- `POST /v1/migrations` `{dataset_id, prefix, to_backend_id?, mode?, cutover?, copier?}` → `202 Migration`, `pending` (apps `Archive.Adopt`; ArchiveAdmin).
  - `prefix`, `to_backend_id` (default and only value: the dataset's own backend) and `mode` as §5.12.
  - `cutover: auto` (the default) flips as soon as every file is verified; `manual` waits at `verified` for `POST …/cutover`.
  - `copier: auto` (the default) runs Storage Mover; `external`: the API client copies to `target_root` + the S3 key and reports its runs (`copier_kind` is `storage_mover` or `external` on the wire, an open enum).
  - Refusals as §5.12. A cancelled migration's prefix may be started again; its copies are found again. Audited as `migration.start`.
- `GET /v1/migrations?dataset_id&state&cursor&limit`, newest first, and `GET /v1/migrations/{migration_id}` → `Migration`: `from_backend_id`, `to_backend_id`, `mode`, `cutover`, `copier_kind`, `state` (`pending → copying → verifying → verified → cut_over`, `failed`, `cancelled`; open), `files` (`total`, `verified`, `failed`, `excluded`), `copier` (Storage Mover job, the last run, runs reported), `journal_checked_through`, `verified_at`, `cutover_requested_at`, `cut_over_at`, `cancelled_at`, `source_deletes_pending` (§5.18), `last_error`, `created_by`.
- **Actions.** `cutover`, `cancel` and `retry` are for the app that started the migration (`Archive.Adopt`) or an ArchiveAdmin session with an AD identity. Another app gets `404 not_found`, as if the migration didn't exist. Each takes `{}` and answers `200 Migration`. A state that refuses the action is `409 migration_conflict`.
  - `POST /v1/migrations/{migration_id}/cutover`: a `manual` migration flips once verified (asked earlier, it waits). Nothing is flipped before every file is verified (decision 24). An `auto` one, or one `cut_over`, `cancelled` or `failed`, is `409`. Asked twice, it answers the same. Audited as `migration.cutover`.
  - `POST /v1/migrations/{migration_id}/cancel`: stops stepping. Nothing is deleted anywhere: files already flipped keep their verified copy, the rest stay served from the source, and a Storage Mover run already under way finishes on its own. `cut_over` or `cancelled` is `409`. Audited as `migration.cancel`.
  - `POST /v1/migrations/{migration_id}/retry`: a `failed` migration goes back to `pending`; a new Additive run copies only what is missing. Another state is `409`. Audited as `migration.retry`.
- `POST /v1/migrations/{migration_id}/runs` `{run_ref, state: running | succeeded | failed, started_at, finished_at?, counters?}` → `202 Migration` (the app that started it; `copier: external` only). Display only: OmniFile verifies every file itself. A run's `started_at` moves the journal gate (nothing flips until the bucket's events are applied past it), and a `running` run sends a `verified` migration back to `verifying`. A report for the same `run_ref` replaces the last one. Audited as `migration.run_report`.
- `POST /v1/migrations/{migration_id}/exclude` `{object_keys (1-1,000), reason}` → `200 Migration` (ArchiveAdmin, session only, re-auth): takes objects that can never be verified out of the bar. Never a partner: lowering the bar is a person's decision (anti-pattern 7). Only live files' objects under the prefix with no verified copy can be excluded; other keys are ignored (the audit counts both). Excluded objects are not flipped and stay served from the source, so the dataset doesn't reach `cut_over` while they live. Audited as `migration.exclude`.

### 5.18 The cutover report (M5, partner; decision 40)

Deletes made through OmniFile while a file's bytes still live in the source are tombstones in OmniFile (decision 30). They are **never applied to the source**: the report lists them so the API client, or the client, can.

- `GET /v1/migrations/{migration_id}/tombstones?cursor&limit` → `{migration_id, state, cut_over_at, source_deletes_pending, applied_by_omnifile: false, items, next_cursor}`. It names S3 keys and paths, never file contents. **The full list is readable by the following, even for files they can't read** (owner, 2026-11-04, an exception to §3.2 for this list only):
  - the app that started the migration;
  - for a migration started in the portal (`created_by` `user:…`), a registered API client with `Archive.Adopt`. An app with only `Archive.Read` gets `404`, and so does any app for another app's migration;
  - a signed-in ArchiveAdmin with an AD identity. One with no on-premises SID reads no rows (§3.3); `source_deletes_pending` still says how many there are.
  - Any other signed-in user gets `403`.
  - **Partner isolation** (the lead's reading of the owner's rule): an API client never reads another client's migration. Even with `Archive.Adopt`, it gets `404` for a migration another app started, as if it didn't exist.
  - Every read is audited as `migration.tombstones.read`, with the row count, including a read that returns no rows.
- One item per source object under the prefix whose file was deleted through an edge (its last tombstone names the edge) and which may still be in the source (a location not marked `deleted`). Deletes the S3 change feed brought in already happened there and are not listed. Each item: `file_id`, `path` (its last one), `source` (`backend_id`, `container`, `key`, `backend_version_id`, `etag`, `verify_etag_before_delete`), `deleted_at`, `deleted_by`.
  - `verify_etag_before_delete` is true when the object has no version id (an unversioned bucket): a delete by key could remove newer bytes, so check the ETag first (DeleteObject with If-Match).
  - `deleted_by` is `{kind: "edge", edge_id, identity_id, proof}`, ids only, never names. `edge_id` is the edge the delete came through. `identity_id` is the catalog identity of the user the edge reported making it (`control-api.md` §4.1 `actor`: the NFS caller, the SMB session's user, or a local process's uid), with the service's `proof`: `proven` (Kerberos NFS on a Kerberos-only share, or SMB on a `proven` share), `claimed` (NFS `sec=sys`, a local process), or `unknown` with `identity_id: null` when the edge could not tell who (its reconcile pass found the delete, say one made while the daemon was down, or an older edge). `kind` and `proof` are open enums.
- The report is live: a file restored (no longer deleted) leaves it. `source_deletes_pending` is its size, also on `migration.cut_over` (§5.11) and on the migration.

### 5.19 Recall batches (M5, partner)

Recalls of paths or a whole dataset by a due date (`ARCHITECTURE.md` §6). Apps with `Recall.Request` only: a batch acts at dataset level, unfiltered by ACL (§2.2), so a signed-in user recalls file by file with `POST /v1/recalls`.

- `POST /v1/recall-batches` `{dataset_id, paths?, due_by?, priority?}` → `202 RecallBatch`.
  - `paths`: up to 100 dataset paths (files or folders, from `/`); absent, the whole dataset. A missing path is `404`; more than 100 paths or 1,000,000 files is `422 batch_too_large`.
  - Per file: with `priority`, the files that offer it use it and the others are `skipped` with a reason. Otherwise the cheapest offered priority whose ETA meets `due_by`; if none does, the fastest, and the file counts as `late`. Without either, `standard`. ETAs and fees come from the file's recall options (§5.5): no time above the no-promise size, no High where it can't be faster (anti-pattern 5). Files already online are `online`.
  - Up to 1,000 files are planned in the request, so the answer carries the `eta` (the last planned file's) and the planned files' retrieval fees.
  - Fees are never added across currencies. A migrating dataset's files can be priced by two providers' tables (S3 and Azure), so `costs` is one `Money` total per currency, ordered by currency code. `cost` is that total when there is one currency, and null when there are none or several. Each item's `fee` is in the currency of the table that priced it. The worker plans larger batches in chunks (`state: planning`, `eta` null until done), then starts or joins each planned file's one active recall (`starting`, then `done`), with the app as requester (`via: api`). Users who open the files get their Teams cards as usual.
  - Audited as `recall.batch`.
- `GET /v1/recall-batches/{batch_id}?cursor&limit` → the batch with `items` (a page of its files by id: `path`, `status` `planned | started | joined | online | skipped`, `priority`, `reason`, `eta`, `late`, `fee`, `recall_id`) and `next_cursor`. Only the app that made the batch reads it; another app gets `404`.

### 5.20 Settings (M6)

The owner asked for one admin place to set up storage, AWS, identity, Teams, email and Storage Mover (2026-11-04). **Settings connect and check; they never store a secret.** Azure is reached with managed identities and RBAC, AWS with web-identity federation (decision 43), Teams with the bot's managed identity. ArchiveAdmin, signed in, never an app. Every change is audited; adding or editing storage needs a sign-in within 10 minutes (§2.4). Live checks call out only with `OMNIFILE_SETTINGS_PROBES=1`, as `web`'s own managed identity (`web/__main__.py` lists the settings it reads).

- **Storage.** `GET /v1/settings/storage` → the backends, what uses each, and whether the deployment is private.
  - `POST /v1/settings/storage` `BackendDraft` adds a backend; `PUT /v1/settings/storage/{backend_id}` edits one. Both build the row with the admin CLI's code (`admin/backends.py`: `azure_row`, `s3_row`), so they refuse what it refuses, and write it through the owner-run `catalog.backend_register` (migration 0035; `web` has no INSERT or UPDATE on the table): idempotent by name, always private, never another kind. The catalog's `backend_guard` keeps what data uses for every writer, with the CLI's own list (`admin/backends.py` `UNDER_DATA_MAY_CHANGE`): on a backend a dataset, source or migration uses, no key is added, changed or removed but S3 buckets added, an inventory rule, a display name and an Azure row's region. Audited as `backend.add` and `backend.update`, with the caller.
  - `GET /v1/settings/storage/{backend_id}/checks` runs the checks now: each account's blob name resolves to private addresses only (decision 22: private means never public), the service identities hold Storage Blob Data Contributor or Owner, public network access is off, versioning and the change feed are on, and the live account is ZRS with Smart while the archive account isn't ZRS. For S3: the account is in the AWS settings, the S3 and STS interface endpoints resolve privately, and the role was assumed by a test in the last 7 days.
- **AWS.** `GET /v1/settings/aws` → the accounts, roles, audience and issuer. `GET /v1/settings/aws/trust-policy?role_arn=` → the exact trust policy for the pasted role: the account's IAM OIDC provider for `https://sts.windows.net/<tenant>/`, `sts:AssumeRoleWithWebIdentity`, `aud` = the deployment's `api://` audience and `sub` = the service identities. Nothing is stored. `POST /v1/settings/aws/test {role_arn}` → `202 SettingsTest`: the worker assumes the role with its own web identity, through the account's configured endpoints, and records only the role and expiry.
- **Identity.** `GET /v1/settings/identity` → tenant, client id, audiences, the app roles and the AD groups that hold them, and admin-consent status with Entra's consent link (only a tenant administrator can grant it). From Graph when `web`'s identity may read the app's role assignments (Application.Read.All, VERIFY); otherwise from who held each role at sign-ins in the last 30 days. `GET /v1/settings/identity/whoami` → the caller as the portal sees them, with checks (SID, groups, role, recent sign-in).
- **Teams.** `GET /v1/settings/teams` → health from the catalog's cards of the last 24 hours, and the app package. `GET /v1/settings/teams/package` downloads the zip `deploy/teams/build.sh` made, when `OMNIFILE_TEAMS_APP_PACKAGE` names one `web` can read (`404` otherwise; the page says how to build it). `POST /v1/settings/teams/test-card {}` → `202 SettingsTest`: the worker sends the caller a card through the notify path.
- **Email fallback.** `GET /v1/settings/email` → the sender mailbox, the caller's mail address and recent sends. `POST /v1/settings/email/test {}` → `202`: the worker mails the caller from the sender.
- `GET /v1/settings/tests/{test_id}` → the caller's own test: `pending`, then `passed` or `failed` with a plain `detail`. The portal polls it every second for up to 30 s. Audited as `settings.*_test` when asked and `settings.test` when done.
- **Storage Mover.** `GET /v1/settings/storage-mover` → the Storage Mover, its agents and projects with their status, and the endpoints the migration engine uses, read-only through ARM.
- **Connect Azure** (`docs/design/cloud-connect.md` §1). `GET /v1/settings/azure` → `AzureSettings`: the steps with their status, the latest listing, the choice, the plan of grants (every role, scope, identity and action), the grants made (active, and removed in the last 30 days), what discovery found, what was confirmed, and the last sign-in operation. `POST /v1/settings/azure/sign-in {purpose, grant_ids?}` → `201 AzureSignIn`: a delegated sign-in to ARM (`openid` and `user_impersonation`, auth code + PKCE, `login_hint` the admin). The browser goes to `authorize_url`; Entra returns to `GET /auth/azure/callback`, which checks the `__Host-omnifile_azure` cookie, the state, the nonce and that the ID token's `oid` is the admin who started it, then does the operation with the access token and drops it: `list` reads tenants, subscriptions and resource groups; `grant` checks the admin's rights on every scope (`…/permissions`) and then PUTs the planned custom roles and assignments; `remove` deletes the named ones. `grant` and `remove` send `max_age=600`. No token is stored. `PUT /v1/settings/azure/choice {subscription_id, resource_groups, mover_resource_group?}` checks the choice against the listing (only the deployment's tenant) and returns the plan. `POST /v1/settings/azure/discover {}` reads, with `web`'s own identity, the storage accounts, VNets, private endpoints and storage movers of the granted resource groups. `POST /v1/settings/azure/confirm` records the ticked resources (only storage accounts reachable privately). Audited: `settings.azure_*_start`, `settings.azure_list`, `settings.azure_grant`, `settings.azure_grant_remove`, `settings.azure_choice`, `settings.azure_discover`, `settings.azure_confirm`.
- **Connect AWS** (§2). `GET /v1/settings/aws/connect?region=&deployment_id=` → `AwsConnect`: the template's parameters filled in, the quick-create link (when `OMNIFILE_AWS_CONNECT_TEMPLATE_URL` names the template in S3), the console's Create stack link and the steps. `GET /v1/settings/aws/connect/template` downloads it. `POST /v1/settings/aws/connect/fetch {account_id, region, deployment_id, endpoints?}` → `202 CloudTask`: the worker assumes the stack's role with its web identity and only reads (buckets, versioning, EventBridge, the queue, and without private networking the stack's outputs); the endpoints pass the operator settings' rules first. `POST /v1/settings/aws/connect/outputs {text}` parses pasted outputs into the account's entry, calling nothing. `POST /v1/settings/aws/connect/confirm` records the entry (`catalog.aws_connection`) and answers the whole `OMNIFILE_AWS_ACCOUNTS` value to set in the deployment (`awsAccounts`); the S3 backend never reads it from the catalog.
- **Storage Mover deployment** (§3). `POST /v1/settings/storage-mover/deploy {name, project, location}` → `202 CloudTask`: the worker PUTs the Storage Mover and its project in the resource group Connect Azure granted it. `GET /v1/settings/storage-mover` also answers `deployment` (with the `OMNIFILE_STORAGE_MOVER` value and the agent's registration values), `can_deploy` and `agent_steps`; before the setting is made it reads the deployed mover's agents. `GET /v1/settings/tasks/{task_id}` → the task (`aws_fetch`, `mover_deploy`).
- **Source scans** (§4). `POST /v1/source-scans {backend_id, bucket, prefix, inventory?, max_objects?, max_seconds?}` → `202 SourceScan`; `GET /v1/source-scans/{scan_id}`. ArchiveAdmin. The worker reads the prefix with the seed's readers (listing or S3 Inventory), one batch of 10,000 a message, and stops at the caps (`capped`). The result: first-level prefixes, the storage-class mix, a Bulk restore estimate (waves of 100,000, AWS's stated hours, the shared 1,000 requests a second, the S3 price table if set), objects over 10 GiB (no time promised) and suggested waves. Nothing is written (decision 40). Audited as `source.scan`.
- **Prices and alerts.** Prices are §5.8's routes, and who sees them §5.24. `GET /v1/settings/alerts` → the alert rules of `observability.md` §1, read-only, with the catalog's view where it has one (edges, failed migrations, the CRL upload) and `unknown` (see Azure Monitor) otherwise.

### 5.21 API clients (M6)

"Whenever any type of API recommendation comes in, it needs to have its owner stated, and they need to have access" (the owner, 2026-11-04). ArchiveAdmin, signed in.

- `GET /v1/settings/api-clients` → `{items, setup}`: every client, and how a client gets a token (token endpoint, scope `<application ID URI>/.default`, the steps). **OmniFile stores no key for an Entra client:** it authenticates with its own app credential, a certificate by preference.
- `POST /v1/settings/api-clients` `{app_id, display_name, scopes, owner_identity_id}` (re-auth) → `201 ApiClient`. `app_id` is the Entra application (client) id. `scopes` are among `Policy.Propose`, `Recall.Request`, `Archive.Read`, `Archive.Adopt`. The owner is a person the catalog knows (`GET /v1/settings/people?q=`). `409 name_taken` for an application registered already. Audited as `api_client.register`.
- `GET /v1/settings/api-clients/{id}`; `PATCH …` `{display_name?, scopes?, owner_identity_id?, enabled?}` with `If-Match`. Disabling needs no re-auth and takes effect on the client's next call; anything else needs re-auth. `POST …/revoke {}`: for good, with its keys. Audited as `api_client.update`, `.disable` and `.revoke`.
- **Keys** (§2.2.1): `POST …/keys` `{scopes?, expires_in_days?}` (re-auth) → `201 {key, api_key}`; `POST …/keys/{key_id}/rotate` `{expires_in_days?, overlap_hours?}` (re-auth) → `201 {key, api_key, previous}`; `POST …/keys/{key_id}/revoke {}` → `200 ApiKey`. Audited as `api_key.create`, `.rotate` and `.revoke`, by `key_id`.
- **Approving a client's proposal.** Nobody answerable for the client approves it: its owner, who registered it, and who made the key it came with get `403 self_approval` (the catalog's `proposal_client_guard` refuses it too). Approval checks again that the client is enabled (`403 client_disabled`) and that its owner can still see the dataset (`403 owner_lacks_access`). Revoking a client marks its pending proposals stale (`stale_reason: client_revoked`).
- **The owner rule.** A proposal (`POST /v1/policies/proposals`) from a client is refused with `403 owner_lacks_access` unless its owner can see the dataset: they held ArchiveAdmin or DataSteward at a portal sign-in in the last 30 days (`identity.app_roles`, written at each sign-in), or they are an Owner of one of its shares (their SID or AD groups). The proposal records the client and owner (`Proposal.api_client`), and the portal shows "From: API · <client> · owner <name>".
### 5.22 Visual views (M6, WP13)

Read-only data for the portal's three pictures: a file's journey along the tiers (in the file drawer), the share map (on the share page) and the policy timeline (in the policy editor; it needs no new route, as it draws the editor's own form and the preview's checks). Everything is computed from the catalog and the backends' `capabilities()` when it is asked for; nothing is stored.

- `GET /v1/files/{file_id}/journey` (read on the file, §3.2; unreadable is `404`) → `FileJourney`:
  - `backend`: the backend that holds the file's primary copy now (the dataset's home if there is no copy yet): `kind`, `role` (`home`, `source` while a migration still serves it from the source, or `other`), the dataset's `storage_layout` and `migration_state`, and the `account` that holds the copy (`live` or `archive` in a two-account layout, `single`, or null on S3).
  - `tiers`: that backend's ladder from its `capabilities()`, warmest first: `{logical, classes: [{native_class, minimum_days, needs_restore}]}`. The portal shows the logical name and puts the provider's class in a tooltip (S3: Standard, Standard-IA, Glacier Instant Retrieval, Glacier Flexible Retrieval or Deep Archive).
  - `pin`: the nearest active pin on the file or a folder above it (`inherited_from`), with `tier`, `until`, `override_archive` and `set_at`.
  - `holds`: the active holds on the file or a folder above it, with `kind`, `until` and `set_at`.
  - **Who set them.** `set_by` and `reason` follow `GET /v1/pins` and `GET /v1/holds` (§5.6): the caller must be an ArchiveAdmin, a DataSteward or the share's Owner, and must be able to read the item the pin or hold is on. A pin or hold on a folder the caller can't read still shows its tier and dates, never who set it or why. A caller who set a pin or hold also sees `set_by` on it. Everyone else gets `null`, and the portal says who could have set it from the role rules.
  - The next move and the archive date are the file's own `next_action`, `next_action_at` and `archive_at` (`GET /v1/files/{file_id}`), which the scheduler sets from the policy and the pins. Recall times come from `GET /v1/files/{file_id}/recall-options` and `GET /v1/recalls/{recall_id}` (§5.5), so no time is shown above the no-promise size.
- `GET /v1/shares/{share_id}/map` (anyone who can see the share, as `GET /v1/shares/{share_id}`; otherwise `404`) → `ShareMap`, over the live files under the share's root, nested folder shares included:
  - `edge`: `{resident, stub, arriving, remote}`, each `{files, bytes, local_bytes}`, by `file.state` (`stub` includes files being filled, `arriving` is `landing` and `uploading`).
  - `stores`: the primary copies per backend (`role` `home` first, then `source`) and per tier: `{logical_tier, native_class, account, files, bytes}`. Files with no committed copy yet are only on the edge.
  - `recalls`: the open recalls of those files, `open` and `by_step` (`requested`, `rehydrating` with `copying`, `downloading`).
  - `home`, `source` (the migration source's kind, or null) and `migration_state`.
  - `migrations`: the dataset's open migrations (`pending` to `verified`), `{migration_id, state, prefix, files: {total, verified}}`, for an ArchiveAdmin; `null` for everyone else, as `GET /v1/migrations` is ArchiveAdmin-only.
  - **RBAC.** `scope: all` for ArchiveAdmin and DataSteward (owner, 2026-11-04): every file counts, and no file is named (§3.2: roles see totals without names). `scope: readable` for everyone else, share Owners included: only files the caller's AD groups can read count, in folders they can read, reached only through folders they can read, so the totals reveal nothing the caller can't browse.
  - **Cost.** Both scopes are summed from the worker's roll-up (`share_map` sweep each minute, `usage/share_map.py`, migrations 0056 and 0057), so a page view never counts files: groups of each folder's live files with one captured ACL, one annex folder, one state and one primary copy's backend, tier and class. The map walks the share's folders (for `scope: readable`, only folders the caller can read) and sums their groups; for `scope: readable` each group goes through the same read predicate its files would, so nothing about who may read is stored. The worker recounts only the folders that changed (a file's folder before and after a move, a folder moved, renamed or deleted, a location's tier, class, backend or state, an annexed file's annex folder or its annex row), a bounded batch at a time, each in its own short transaction. For `scope: readable`, a folder with a change waiting isn't summed from the roll-up: its files are counted live, each through the read predicate, so a change of a file's ACL, folder or annex folder counts at once, with or without the worker; past 20,000 files in such folders under the share, at their last count, the map says `counting` rather than a stale total.
    - `basis: rollup` and `computed_at`: the time the counts are true as of: for `scope: all`, the oldest count summed among folders with a change waiting, and now for the rest; for `scope: readable`, now (those folders are counted live). A worker that falls behind makes it older, never a wrong total current (alert `share-map-stale` past two hours, gauge `omnifile.share_map.age`).
    - `basis: counting`: a folder it would sum hasn't been counted yet (a new share, or the first count after the update), or, for `scope: readable`, the folders with a change waiting held more than 20,000 files at their last count, so `edge` is null and `stores` empty, never a total of zero.
    - `basis: none`: the `scope: readable` count ran out of its 3 s (`edge` and `recalls` null).
    - `recalls` is always live, from the active-recall index. Both routes are on the preview rate limit. The portal keeps an answer for a minute.
- `GET /v1/shares/{share_id}/map/files?edge&tier&role&recalling&cursor&limit` (as above) → `{items: [MapFile], next_cursor}`: the files behind one node of the map, by `file_id`, `limit` 1 to 200 (default 50). Name a node with `edge` (`resident`, `stub`, `arriving`, `remote`), `tier` (a logical tier, optionally with `role` `home` or `source`), or `recalling=true`; with none, `400`. A `MapFile` is `{file_id, name, parent_id, size, state, resident_bytes, tier}`. It names files, so it lists **only files the caller can read, in folders the caller can read** (walked only through readable folders, as browsing is), whatever their role, ArchiveAdmin and DataSteward included. A listing that runs past 3 s is `503 unavailable`.
### 5.23 Filers and annexes (annex A1, partner)

An annex watches a directory on an existing NAS and copies its files to Azure as plain blobs, without sitting in the users' data path (`docs/design/annex.md`, decision 51). Phase A1 has **Copy mode only**: nothing is ever written to the NAS, and a file deleted there is reported, never mirrored. Every enum below is open (`x-extensible-enum`): Archive after N days, Move now and Custom come with later phases, and so do vendor feeds.

- `GET /v1/filers` → `{items: [Filer]}`; `POST /v1/filers` `{name, host, protocol: smb | nfs3 | nfs4, export, edge_id, vendor?: generic | ontap | powerscale, credential_mode?: machine_account | service_keytab | sec_sys, event_source?: scan | smb_notify}` → `201 Filer`. ArchiveAdmin registers (audited `filer.add`); ArchiveAdmin, DataSteward and apps with `Archive.Read` or `Archive.Adopt` read. ONTAP and PowerScale are generic NFS/SMB servers in v1. `sec_sys` is NFS only, and such a filer is `host_trusted`. Each filer gets one backend row of kind `nas`; the service never reads NAS bytes.
- `GET /v1/filers/{filer_id}` → `Filer`; `GET /v1/filers/{filer_id}/health` → the edge's last mount check (`ok`, `read_only`, `readable`, `sec`, `error`, `checked_at`) and how many annexes use the filer.
- `POST /v1/annexes` `{name, filer_id, path, dataset_id, dataset_path?, owners_sid, mode?: copy, settings?, owner?}` → `201 Annex`. An ArchiveAdmin, or a DataSteward in the Owners group; apps with `Archive.Adopt`, naming the person who owns the annex (`owner`, a user SID in the Owners group, which must be a group, not that SID: `422 owner_required`, `400 invalid_request`). For an app both that owner and the API client's own owner must be able to see the dataset as in §5.21 (`403 owner_lacks_access`), and an app's rule set stays `proposed` (`ruleset_state`), with the edge not starting the annex, until a person approves it. Any mode but `copy` is `422 mode_not_available`. The dataset must be an Azure dataset on the filer's edge with no shares (`409 dataset_shared`); one directory has one annex (`409 source_conflict`). The rule set is the fixed Copy template, approved at creation by the Owners when a person creates it. Settings default from `GET /v1/settings/annex-defaults`. Audited as `annex.create`.
- `GET /v1/annexes` → `{items: [Annex]}`: ArchiveAdmins and DataStewards see all, the Owners group theirs, apps with `Archive.Read` or `Archive.Adopt` all. `GET /v1/annexes/{annex_id}` → `Annex` with `counts` (present, copied, verified, pending, gone, purged, bytes, problems in 30 days), its `filer`, `can_manage` and `can_purge`, and an `ETag`.
- `PATCH /v1/annexes/{annex_id}` `{state?: active | paused | retired, settings?, deleted_retention_days?}` with `If-Match`: Owners and ArchiveAdmins; for an app, retiring included, its client owner must see the dataset (`403 owner_lacks_access`). A deleted-file retention (purge copies of files deleted on the NAS after N days; `null`, the default, keeps them until an ArchiveAdmin purges them) is ArchiveAdmin only, with a sign-in in the last 10 minutes (`401 reauth_required`). A retired annex stays retired. Audited as `annex.settings`.
- **Rule sets (phase A2.1).** A rule set is `{rules: [{id, label?, trigger, conditions, actions}]}` (annex.md section 4.1): triggers `arrived`, `changed`, `age`, `deleted_at_source`; conditions `path` (globs), `extension`, `size_gte`, `size_lt`, `age_since_arrival_days_gt`, `modified_age_days_gt`, `accessed_age_days_gt`, `owner_sids`, `group_sids`, `content_type`; actions `exclude`, `copy_to`, `remove_from_source`, `remove_after_days`, `policy`, `tag`, `keep_copy`. The first matching rule of a trigger wins, and each trigger in use ends with a default rule. The edge evaluates rules, for the preview and for real, and fails closed on a rule set it can't read. **Phase A2.1 removes nothing from any NAS**: a removing rule produces `would_remove` events only.
  - `GET /v1/annexes/{annex_id}/rulesets` → `{items: [AnnexRuleset], next_cursor}` (version, state, mode, `removes`, `needs_archive_admin`, the person who owns it, how it was proposed, rationale, the decision, its dry run); `GET …/rulesets/{version}` adds the body, today's `checks` and the dry run.
  - `POST /v1/annexes/{annex_id}/rulesets` `{body, rationale?, owner?}` → `201 AnnexRuleset`: a proposal, never applied by itself. The annex's Owners and ArchiveAdmins; apps with `Archive.Adopt` naming its owner (as for `POST /v1/annexes`). A rule set failing the section 4.2 checks (`missing_default`, `unreachable`, `remove_without_copy`, `remove_without_age`, `remove_before_quiet`) is `422 rules_check_failed`. It gets a dry run at once.
  - `POST …/rulesets/{version}/approve` and `…/reject` `{note?}` with `If-Match` → `200 Annex`. A person, signed in. A proposal whose rule set removes anything (`needs_archive_admin`) is an ArchiveAdmin's to approve; the others the annex's Owners'. It is computed again at approval against the running set. Every proposal moves the annex's ETag. Nobody approves their own: not the person a proposal names (`403 self_approval`), nor anyone answerable for the app it came from. At approval the named person must still be an Owner or see the dataset, and an app's client must still be enabled with its owner seeing the dataset (`403 owner_lacks_access`, `client_disabled`); the checks run again. Approval supersedes the running version and any other proposal, and sets the annex's `mode`. A removing rule set that adds or widens a removal, or changes any `arrived`, `changed` or `age` rule, starts a dry run of `dry_run_days` (default 7, decision 5) in `removals.dry_run_until`. `GET …/rulesets/{version}` gives `running_version` and `running_body` to compare with. Audited as `annex.ruleset.propose`, `…approve`, `…reject`.
  - `POST /v1/annexes/{annex_id}/dry-runs` `{body?}` → `202 AnnexDryRun` (`requested`, with the body's `checks`): a preview of a draft, or of the running rule set, computed by the edge over its listing within seconds. `GET …/dry-runs/{run_id}` → its `report`: per rule the files and bytes it decides, what would be copied, excluded or undecided, and when files would leave the NAS (now, then per day for a year). Sample paths are NAS paths: ArchiveAdmins and apps get them, others the counts. The listing is unfiltered and an annex's folders don't carry the NAS's ACLs yet, so previews are ArchiveAdmins' and apps': anyone else gets `403 role_required` for one, and `report: null, report_hidden: true` for a run's report (annex.md section 4.5). States: `requested`, `running` (the edge took it), `done`, `failed`. A newer draft from the same person replaces their older ones; a run not taken in 10 minutes or answered in 15 fails. At most 20 wait per annex (`429 throttled`), counting proposals' runs (a proposal past the limit has none, and the approver's page asks for one).
  - **Modes at creation:** `POST /v1/annexes` takes `mode: archive_after` (with `archive_after_days`), `move_now` or `custom` (with `rules`). A person's annex copies at once under the Copy template, and the mode's rule set waits as a proposal; an app's annex waits whole. Pointer files default on for Archive after N days.
  - **Removal settings** (what phase A2.2 obeys): `PATCH` takes `removals_paused` (any Owner pauses; resuming is an ArchiveAdmin's, with a recent sign-in), `pointer_files`, and, ArchiveAdmin only, `grace_minutes`, `removal_rate_per_hour` and `removal_window` `{start, end, tz?}`. `GET /v1/settings/annex-defaults` and `PUT` add `removals_paused` (pause all removals; resuming needs a recent sign-in), `removal_rate_per_hour`, `grace_minutes` and `dry_run_days`. `Annex.removals` shows them, with `enabled: false` until A2.2.
- **Removals (phase A2.2; decision 59).** Removals never start by themselves.
  - `POST /v1/annexes/{annex_id}/removals/confirm` with `If-Match` → `200 Annex`: after a removing rule set's dry run has ended (`409 version_conflict` before), an ArchiveAdmin who reviewed it confirms, signed in within the last 10 minutes (`401 reauth_required`). It can't be the person who proposed the rules, nor anyone answerable for the app they came from (`403 self_approval`). The confirmation is for the running version; a new version needs its own. Audited as `annex.removals.confirm`, with how many files the dry run would have removed.
  - `Annex.removals.enabled` is then true unless a pause stops it.
  - `GET /v1/annexes/{annex_id}/removals?state=pending|done|cancelled` → `{items: [AnnexRemoval], live, why_not}`: each removal, its steps (hidden, deleted, the pointer file) and why one was cancelled, filtered by the files' captured ACLs.
- `GET /v1/annexes/{annex_id}/events?kind&since&until&cursor&limit` → `{items: [AnnexEvent], next_cursor}`, newest first. For an app, here and on `files` and `problems`, the API client's owner must see the dataset (`403 owner_lacks_access`); otherwise apps read at the dataset level (§2.2). A user sees an event about a file only if the file's captured NAS ACL lets them read it (decision 28); an event naming a NAS path without a catalog file (skipped, drift) shows its kind and reason to everyone who sees the annex, and its path and details to ArchiveAdmins and apps only (access-control.md §5).
- `GET /v1/annexes/{annex_id}/files?path&state=present|gone|purged&cursor&limit` → `{items: [AnnexFile], next_cursor}`, by path, filtered by who may read each file: its captured ACL and the folders above it on the NAS (decision 63). `copy_state` is `pending`, `copied`, `verified`, `changed` (a newer version waits) or `purged`. `folders_narrow` lists the folders above the file (export-relative, `""` its root) that don't let everyone through or haven't been read.
- `GET /v1/annexes/{annex_id}/timeline?days=30` → arrivals, departures, changes, copies and verifications per UTC day, and the baseline (the files present when the annex started, never counted as arrivals).
- `GET /v1/annexes/{annex_id}/problems` → drift from the weekly reconciliation (missed arrivals and departures, changes with no event, copies that no longer match) and the copier's problems in the last 30 days (skipped, failed, unreadable ACLs, refused purges). `drift_counts` gives the unresolved drift by kind to everyone who sees the annex; the drift rows, which name NAS paths, are ArchiveAdmins' and apps' (others get an empty list). `read_set` `{ready, filer_answered, read_at, folders, unreadable}` (decisions 63 and 65; `filer_answered` false: the filer's access question is unanswered, and every file is hidden): whether the edge has read every folder above the annex's files, up to the export's root (until then they are hidden from everyone), and how many folders' permissions can't be read (the files below them are hidden). `copies_refused`: the files still on the NAS whose copy Azure's read-back refuses, however long ago, stopped ones first, each with `at_bound` (refused 3 times in a row: the service no longer asks for it), `alerting` (the alert `annex-copies-refused` is up for it: at the bound, or stopped whatever its count, and not acknowledged), `copy_refusals`, `copy_refused_at`, and `copy_stopped` and `copy_refused_ack` (`{by, at}` or null). A stopped file stays there until a version verifies or it is resumed. ArchiveAdmins and apps get every one; others the files they may read. At most 200 are listed, and `copies_refused_total` says how many there are.
- `POST /v1/annexes/{annex_id}/files/{file_id}/purge` → `202 AnnexFile`: ArchiveAdmin only, with a sign-in in the last 10 minutes (`401 reauth_required`); the portal asks to confirm first. Only a file gone from the NAS (`409 file_present` otherwise). The worker prunes its versions and deletes their blobs; WORM retention and legal holds still refuse it: the request ends, and `purge_refused_at` on `AnnexFile` and a `purge_refused` problem record it. It can be asked for again, and the file can be returned meanwhile; either clears it. Asking again while a request is open queues it again. Audited as `annex.purge.request` and `annex.purge`.
- `GET /v1/settings/annex-defaults` (ArchiveAdmin, DataSteward) and `PUT` (ArchiveAdmin, with `If-Match`) `{quiet_minutes, quick_scan_minutes, full_scan_minutes, reconcile_days}`: what new annexes start with.

### 5.25 Getting an annexed file back (annex A3)

A file an annex removed from its NAS (or one the NAS lost in Copy mode) can be placed back where it came from, in a restore folder, or in an OmniFile share (annex.md sections 3.6 and 4.6). The annex's **return rules** decide where: rules with the trigger `recall_completed` in the same rule set as §5.23's. A placement waits as `ready` until its writer takes it: the service for a share (phase A3.2, below), the edge for a NAS folder (phase A3.3).

- **Return rules.** Conditions `path`, `extension`, `size_gte`, `size_lt`, `owner_sids`, `group_sids` (the file's) and `requester_in` (the requester's SID or groups). Actions `place_at` (a location), `on_conflict` (`same_content`, `restored_name` (the default), `overwrite_keep_version`), `hold_days` (0 to 365; outbound rules skip a placed file until then), `notify` (`requester`, `owner`, `{group_sid}`), `let_requester_choose` (up to 20 more locations), or `exclude` (placed nowhere). Locations: `{kind: nas_folder, origin: true}` (its own folder and name), `{kind: nas_folder, path}` (this annex's folder), `{kind: nas_folder, annex_id, path}`, `{kind: nas_folder, filer_id, path}` (relative to the export), `{kind: share_folder, share_id, path}`; a folder keeps the file's folders below the annex unless `keep_relative_path: false`. The service evaluates return rules, never the edge. Templates: "Back where it came from" (`origin`, `restored_name`, 14 days' hold), "Into a restore folder" and "Into an OmniFile share" (`overwrite_keep_version`); Copy and Archive after N days start with the first, Move now with none until a restore folder outside the annex is chosen. **Replacing on a NAS (A3.4, decisions 82 to 85).** `overwrite_keep_version` replaces a file on a NAS only at its own name in its original folder, and only when the file there is exactly the verified copy; anywhere else, or otherwise, a `-restored` name (`outcome: overwrite_refused_<why>`). A rule set lists the rules that can do it in `replaces_on_nas`, which the approval page marks "replaces files on the NAS". `POST /v1/placements` takes an older `version_id` for the origin of a file still on the NAS when the rule says overwrite (else `file_present`); `return-targets` says so with `replace_older`. A placement's `replace` says what became of the file that was there (`state`, `refused`, `replaced_version_id`, `hidden_name`, `kept_as`), and its outcome may be `replaced` or `replaced_old_kept` (the old file kept as `<name>-replaced-<UTC time>`, with a Problem).
- **Checks** (`422 rules_check_failed`): `return_into_drop_folder` (a target a Move-now rule empties again at once), `return_without_hold` (a target a removing rule covers, with no hold), `overwrite_outside_annex` (a NAS file is replaced only inside this annex), `unknown_location` and `location_unreachable` (an annex, filer or share that doesn't exist, a NAS folder on another edge or on a filer with no annex, a share the proposer can't write to). A proposal adding a target outside the annex's own folders is an ArchiveAdmin's to approve.
- `GET /v1/files/{file_id}/return-targets[?requester=SID]` → `ReturnTargets`: for an annexed file the requester may read (its captured NAS ACL decides; `404` otherwise), the rule that decided and each location it allows, default first, with `allowed` and `why_not`: the requester's own right to add a file there, because the edge writes with its own account. A NAS folder is checked against its captured ACL (deny-first), a share folder against the share's Owners and Writers groups and the folder's ACL; anything not known is a no. Also `name_taken` (as far as the catalog knows) and `free_bytes` (as the edge last saw it). The origin is offered only for a file that isn't on the NAS. An API client with `Recall.Request` names the person (`422 requester_required`), and its owner must see the file's dataset, as on the other annex routes (`403 owner_lacks_access`, checked before anything about the file is looked at); the same holds for `POST /v1/placements` and the return preview.
- `POST /v1/placements` `{file_id, version_id?, place_at: "default" | ReturnLocation, on_conflict?, priority?, note?, requester?}` → `201 AnnexPlacement` (or `200` with `joined: true` for an open placement of the same file to the same target). A target the rules don't allow, one the requester can't write to, or an `on_conflict` harsher than the rule's is `403 target_not_allowed`; the origin of a file still on the NAS is `409 file_present`. A file in Archive needs `priority` (one of its recall options) and starts the usual recall (`recall_id`, state `rehydrating`); an online one goes straight to `ready`. Audited as `annex.placement.request`, with an annex event `recall_requested`.
- **Into a share (phase A3.2).** A placement whose target is a share folder is carried out by the worker as soon as it is `ready`. The gates are checked again (the requester can still read the file and add one there). The copy is server-side, into the share's dataset, and is read back in Azure, so its CRC64-NVME must equal the catalog's (`crc_mismatch` otherwise). It becomes a file with `origin = annex_restore`, with the annexed file's own read set, never the folder's. A file whose NT or NFSv4 ACL has a deny isn't offered for a share (`why_not`), because a share's POSIX ACL can't make a group's deny win. The name: a free name is used as it is. With `same_content`, the same bytes already there are left (`outcome: already_there`). With `overwrite_keep_version`, a new version of the file is made, but only when its readers are all readers of the annexed file; otherwise the copy takes a `-restored` name (`overwrite_refused_acl`). With `restored_name`, `-restored` and then a UTC time; all taken is `name_taken`. The copy is held online for `hold_days` (a Keep online hold) and recalled at once for the requester, whose usual recall card says where it is. The source and the share must be on the same backend (`different_backend`). Audited as `annex.placement.placed` (or `…failed`), with an annex event.
- `GET /v1/placements/{placement_id}` → `AnnexPlacement`: the requester's, the API client's that asked, and ArchiveAdmins'. States (open): `requested`, `rehydrating`, `ready`, `writing`, `verifying`, `securing`, `placing`, `stuck`, `placed`, `failed` (`fail_reason`), `cancelled`.
- `POST /v1/placements/{placement_id}/cancel` → `AnnexPlacement`: the way out of a placement that can't go on, for its requester or an ArchiveAdmin. The edge cleans up what it made when it next hears. One that has its `go` can't be cancelled (`409 placement_not_cancellable`), because the file may already be under its name. Audited.
- `POST /v1/placements/{placement_id}/abandon` → `AnnexPlacement`: the way out of a placement stuck after its `go`, an ArchiveAdmin's, once the edge has said nothing new for 24 hours (`409 placement_not_abandonable` otherwise, or before its `go`). It ends as `failed: abandoned`, audited, with a `problem` event. Whatever the edge reports later is refused and audited. The next scan finds the file on the NAS like any other, so the worst case is a recopy, and the file has no hold.
- `POST /v1/annexes/{annex_id}/files/{file_id}/clear-breaker` → `AnnexFile`: an ArchiveAdmin or the annex's Owners looked at a file the loop breaker pinned (more than three returns to its origin in 30 days; `pinned_by_breaker` on `AnnexFile`). It may leave the NAS again, and the breaker counts returns from now. Audited, with `was_pinned`, so a call on a file that wasn't pinned says it did nothing.
- `POST /v1/annexes/{annex_id}/files/{file_id}/copy-stop`, `…/copy-resume` and `…/copy-refused-ack` → `AnnexFile` (#81's follow-up; annex.md section 3.3): a copy Azure's read-back keeps refusing. ArchiveAdmin only, with a sign-in in the last 10 minutes (`401 reauth_required`), each audited (`annex.copy.stopped`, `annex.copy.resumed`, `annex.copy.refused_ack`, with whether it was already so). Only a file whose copy was refused (`409 copy_not_refused`; resume aside).
  - **Stop** copying it again: its records say `copy_stopped` (control-api.md §4.7), so no reconciliation copies the same bytes again, the edge's own every `reconcile_days` included, and a later refusal asks for none. A file changed on the NAS is still copied, as a new copy. **Resume** undoes it.
  - **Acknowledge** the refusals: the file leaves the gauge `omnifile.annex.copies_refused`, so the alert quiets for it alone; it stays on Problems. A new refusal ends the acknowledgement unless copying is stopped: a file still copied again can be refused again, and then the alert comes back.
  - **The gauge** counts a file at the bound, or stopped whatever its count, until it is acknowledged. A file stopped at its first refusal is never asked for again, so it would never reach the bound: it alerts until acknowledged. A file with no verified copy is silent only after two audited acts.
  - Both end when a version of the file verifies, a verified version placed back under its name included. `AnnexFile` carries `copy_refusals`, `copy_refused_at`, `copy_stopped` and `copy_refused_ack`.
- `PATCH /v1/annexes/{annex_id}` `{placements_paused}` and `PUT /v1/settings/annex-defaults` `{placements_paused}`: the placements' kill switch, an ArchiveAdmin's, audited.
- `PATCH /v1/filers/{filer_id}` `{placement_sid}` → `Filer`: which account places files on this NAS, or an explicit `null` to clear it (`{}` is refused). **Every SMB filer needs it:** the staged file's owner is whoever placed it, so until that account is registered every placement fails `placement_account_not_registered`. An ArchiveAdmin's, freshly signed in (`401 reauth_required`), with `If-Match` (`428`, `412`), audited with the old value and what the edge reported. At `go`, only this account's entries count as the file's own owner's.
- `PATCH /v1/filers/{filer_id}` `{access: {limited, groups}}` (decision 65; with or without `placement_sid`) → `Filer`: whether the filer's SMB share or NFS export lets in fewer people than the files' permissions do, which no mount can read. `limited: true` names the AD groups it lets in (at least one, never Everyone); `limited: false` names none. Same rules as above (ArchiveAdmin, fresh sign-in, `If-Match`). Each answer is kept with who gave it and when (`Filer.access`) and audited as `filer.access` with the old answer. An annexed file's readers are only members of those groups, and while a filer is unanswered its annexed files are hidden from everyone.
- `PATCH /v1/filers/{filer_id}` `{acls_hidden_acknowledged: true}` (decision 64) → `Filer`: an ArchiveAdmin acknowledges that the filer's NAS shows no ACLs through its mount, so access there rests on mode bits alone. Only while it is flagged (`Filer.acls.hidden_since`); same rules as above; audited as `filer.acls_hidden`. Until then `Filer.acls.blocks_grants` is true, and placements and share copies of its files that anyone but the owner could read fail `acls_hidden_filer`. A mount report that the ACLs show clears the flag and the acknowledgement.
  - **The edge reports, a person registers.** Only the SID this filer's own edge currently reports can be registered, and the report never registers anything by itself. The `400` says which it wasn't: `well_known`, `no_report` (or the edge can't name it), `stale_report` (none for 6 hours; the edge reports hourly and when its unit starts), `different_sid`, `not_an_account` (a group, say), `not_smb`.
  - **What a person sees first:** `placement_account_report` on `Filer`: `sid`, `kind`, `name` (the principal the edge holds), `error`, `reported_at`, `can_register` and `why_not`. Only an ArchiveAdmin signed in as a person gets it (`null` for others), since nobody else can act on it. A screen shows the account's name and SID, asks an ArchiveAdmin to confirm it, and sends that `sid`; with `can_register` false it says `why_not` instead. `POST /v1/filers` takes no `placement_sid`, because a new filer has no report yet.
- Retiring an annex ends its open placements, and any other annex's that target its filer when no other annex is left there (`failed: annex_retired`, with a `problem` event each). A staged copy stays in the placer's staging folder on the NAS, open only to the placement and read accounts, until the NAS's owners remove it, and a placement that had its `go` may be under its name; the event says which and where.
- `POST /v1/annexes/{annex_id}/return-preview` `{body?}` → `AnnexReturnPreview`: the return rules (a draft's, or the running set's) over the annex's removed files, at most 200,000 (`truncated`): per rule the files and bytes it takes and `may_take` (a rule naming requesters), per target the names taken today and the free space. Counts can tell what is in folders the caller can't read, so ArchiveAdmins and apps only (`403 role_required`).

### 5.24 Who sees prices (WP18)

An ArchiveAdmin chooses which portal roles see costs (Settings, Prices and alerts). The code is `web/prices.py`; the setting is `catalog.price_display` (migration 0043).

- `GET /v1/settings/prices` (ArchiveAdmin, session only) → `PriceDisplay` `{show_to, set_by, set_at}` with an `ETag`. `show_to` lists the roles that see prices: `ArchiveAdmin`, `DataSteward` and `others` (every signed-in user with neither admin role). All three by default; an empty list hides prices from everyone.
- `PUT /v1/settings/prices {show_to}` with `If-Match` (ArchiveAdmin, session only, with an AD identity) → `PriceDisplay`. Audited as `settings.prices.visibility` with `from` and `to`.
- A user sees prices when any of their roles is listed. `GET /v1/me` says so in `prices_visible`.
- For a user who doesn't, `web` removes every amount from every answer: each `Money` becomes null, and a list of them loses its entries. One rule in the endpoint wrapper covers every priced route (the policy preview, recall options, pin previews, checks, usage, recall batches). `GET /v1/price-tables/{kind}` is `403 forbidden` for a DataSteward then. ArchiveAdmins' price editor still reads the rates it edits.
- Partner applications are not affected: their contract carries the amounts.
- Each replica caches the setting for 10 seconds; the one that changes it forgets its copy at once.

The price editor (§5.8) is the same `PUT /v1/price-tables/{kind}` with `If-Match`. `GET` now adds `list`, the built-in table, so the editor can put one rate or a class back to its list price; `DELETE` puts the whole table back.

### 5.22 The migrations console and staged plans (M6, WP12)

The portal's operations view of migrations (migration 0037). Everything is read from the catalog. `web` never calls a provider, and never scans a migration's objects:
- the Storage Mover agents are as the worker last listed them through ARM, every 5 minutes (`catalog.mover_agent`, following ARM's `nextLink` pages);
- each migration's bytes, current failures, archived and expiring objects are counted by the worker every 5 minutes (`migrate/progress.py`), with a sample of its verified bytes for the throughput.

- `GET /v1/migration-console` (ArchiveAdmin, session only) → `{computed_at, migrations: [ConsoleRow], attention: [AttentionItem]}`.
  - Each row is a `Migration` plus `dataset_name`, `bytes` (`total`, `verified`), `counted_at`, and `failing_now`: copies that failed their check whose file has no verified copy yet (`files.failed` only grows). `throughput` is the difference between the worker's samples of the last hour, at least 10 minutes apart, or null.
  - `eta` is `{at, basis}`. `at` is computed from the last hour's rate over the bytes left. It is null, with the reason in `basis`, when nothing was verified in the hour or while objects are still in an S3 archive class without a finished restore. No time is promised beyond the data.
  - Also `health` (`ok`, `attention`, `failing`, `done`, `cancelled`; open), `restores_expiring`, `archived_waiting`, and `next_wave`: the next planned wave of the dataset.
  - These three come from the worker's count, every 5 minutes, and each `Migration` carries the same numbers as `stuck_copies`. `stranded` and `no_checksum` read both queues whole, so the worker looks only when there is a reason: a prefix stopped making progress, an object was excluded since it last looked, or 55 minutes have passed, so at least hourly with a sweep every 5 minutes. A source that keeps making progress can't hide one.
  - `attention` lists what needs a person, each with its fixes:
    - `verify_failed`: copies failed their check and have no verified copy yet (`failing_now`). Fixes: Copy again, or Exclude (§5.17, re-auth).
    - `check_given_up`: copies whose check went to the `migrate_verify` dead letters, whichever prefix sent the letter. No fix here, since a new run doesn't revive a pending copy: an administrator sends them again (`deploy/azure/README.md`, Dead letters).
    - `copy_stranded`: pending copies with no check queued and no letter, whose S3 checksum is known. No fix here, for the same reason.
    - `no_checksum`: pending copies whose S3 objects have no checksum on record, with nothing computing one (SSE-C, or no Batch Operations). Fix: Exclude.
    - `run_failed`: Copy again.
    - `restore_expiring`: restored S3 copies expire within 24 h unverified. Fix: Restore again.
    - `access`: a `last_error` naming access or a private endpoint. Fix: a deep link to the Settings checks.
    - `failed`: Retry.
    - `agent_offline`: an agent is not Online, or wasn't read for 15 minutes. Fix: the Agents view.
    - `seeding`: wait. A planned wave starts by itself once its source is catalogued.
- `GET /v1/migrations/{migration_id}/stuck-copies?kind=stranded|no_checksum` (ArchiveAdmin, session; `cursor`, `limit`) → `{migration_id, kind, count, counted_at, list_limit, items: [{object_key}], next_cursor}`: which objects those two numbers are, for the Exclude dialog.
  - The worker writes the list each time it looks (`catalog.migration_stuck_copy`, migration 0047), because only it can tell: the answer needs the verify queues, which `web` never reads, and the source's binding. So `web` reads a page by key, with no count: `count` is the worker's number.
  - The worker lists at most `list_limit` (10,000) of each kind, the first by key. When `count` is higher, excluding them lets the next ones show at the next sweep.
  - An object excluded since the worker last looked is left out at once. A copy checked since then can still be listed until it next looks; Exclude ignores a key whose copy is verified.
  - It names objects an ArchiveAdmin may not be able to read, so every read is audited (`migration.stuck_copies.read`), like the cutover report, and an ArchiveAdmin with no AD identity gets no rows (section 3.3). The console's other reads name no objects, and aren't audited.
- `GET /v1/migrations/{migration_id}/agents` (ArchiveAdmin, session) → who works on it:
  - the Storage Mover job, its last run and the agents (`agentless: true`: a cloud-to-cloud job runs without one);
  - the dataset's edge, from its heartbeat;
  - restore waves (state, restored and open counts, the first expiry);
  - checksum jobs;
  - verification counts. No file names.
- `GET /v1/migration-agents` (ArchiveAdmin, session) → `{agents, jobs, polled}`: each agent, and each migration under way with its job, run, edge, and running waves and checksum jobs.
- `POST /v1/migrations/{migration_id}/rerun` `{}` (**Copy again**) and `POST …/restore-again` `{}` (ArchiveAdmin, session) → `200 Migration`.
  - Copy again sets `rerun_requested_at`. At its next step the engine starts a new Additive run, which also copies again what failed its check. A run moves the journal gate, as any run does.
  - Restore again sets `restore_requested_at`. The engine's next restore wave also renews restored copies that expire within 24 h unverified.
  - `409 migration_conflict` for an external copier, or a migration that is `cut_over`, `cancelled` or `failed`; Copy again also for one that is `verified` (nothing to copy).
  - For a plan wave's migration, both wait for the wave's window and blackouts (below).
  - Audited as `migration.rerun` and `migration.restore_again`.
- **Plans.** `POST /v1/migration-plans` `{dataset_id, name, owner_identity_id?, waves: [{name?, prefixes, start_at, window?, blackouts?, after?, cutover?, copier?, bandwidth_mbps?, egress_gb_per_day?}]}` → `201 MigrationPlan`.
  - **States.** A signed-in ArchiveAdmin's plan is `approved` at once. An API client's (`Archive.Adopt`) is `proposed`, until an ArchiveAdmin other than the proposer approves or rejects it (anti-pattern 7).
  - **Owner.** `owner_identity_id` is an AD user in the Owners or Writers group of every source the plan touches. An API client must name one; a signed-in ArchiveAdmin defaults to themself.
  - **Prefixes** are `''` or end in `/`. Each lies inside a source of the dataset; a source still being catalogued is allowed, and its wave waits. None overlaps another prefix of the plan or a migration under way (`409 migration_conflict`).
  - **Window and blackouts.** `window` is `{start, end}` in UTC `HH:MM`; an end before the start wraps past midnight. `blackouts` are `{from, to}` ranges. They must leave a minute to start within 60 days of `start_at`.
  - **Dependencies.** `after` names an earlier wave of the plan, which must be verified first.
  - **Copier.** `copier: external` is refused for now (`422 action_not_allowed`): an external copier reports its runs as the app that started the migration, and a plan's migrations are the plan's. Start such a migration with `POST /v1/migrations` instead.
  - Audited as `migration_plan.create`.
- **The wave's migrations are the plan's** (`created_by` `system:plans`, and `plan: {plan_id, seq}` on the `Migration`). Their cutover, cancel and retry go through `POST …/waves/{seq}/cutover`, `…/retry` and `…/cancel` (ArchiveAdmin, session); the migration's own routes answer `409 migration_conflict`, and the app that proposed the plan gets `404`.
- **The scheduler** (the worker, every minute) starts a wave by making one migration per prefix. It does so once its `start_at` has passed, the time is inside its window and outside its blackouts, the wave it waits for is verified, and its sources are catalogued; until then the wave is `waiting`, with `waiting_reason`. A wave's migrations always hold at `verified`. For an `auto` wave the scheduler asks for each cutover itself, only for verified migrations, and inside the window and outside blackouts. The engine checks again and flips nothing unverified (decision 24). A `manual` wave waits for `POST …/waves/{seq}/cutover`. Every change to a wave is one transaction, so a crash leaves each wave whole.
- **The waves of a cancelled plan** that had started go on: they are followed, and an `auto` wave still cuts over in its window; cancel a wave with `…/waves/{seq}/cancel`.
- **Wave states.** `verified` once every migration not cancelled is, and at least one is; `failed` while one is failed, and back to `started` once retried; `cancelled` when every migration was. A wave that waits for a cancelled or blocked wave is `blocked` and doesn't start, so its plan stays open, showing it. A blocked wave can be rescheduled (`PATCH`, changing or dropping `after`, which unblocks it) or cancelled with the plan.
- **What the window and blackouts gate, exactly:**
  - starting the wave;
  - its cutover: the scheduler asks only inside the window, and a migration that leaves `verified` drops the request;
  - the engine's new Storage Mover runs (scheduled reruns and Copy again) and new S3 restore waves (Restore again included) for the wave's migrations.
- **Not gated:** a run or restore already under way, and per-file verification, which reads only inside Azure.
- **Advisory** (recorded and shown, not applied): `bandwidth_mbps` (Storage Mover's agent upload-limit schedule is set in Azure) and `egress_gb_per_day`. S3 Batch Operations has a job priority, not a concurrency cap.
- `GET /v1/migration-plans?dataset_id`, `GET /v1/migration-plans/{plan_id}`: an app sees only its own plans.
- `POST …/approve` and `…/reject` take `{note?}`. `…/cancel` `{}` cancels the waves not started; started ones go on.
- `PATCH /v1/migration-plans/{plan_id}/waves/{seq}` `{base_version, start_at?, window?, blackouts?, after?, cutover?}` (the schedule view's drag, or its form). It applies only to a wave not started (`planned`, `waiting` or `blocked`).
  - A wave may not start before the wave it waits for, nor after a wave that waits for it.
  - A stale `base_version` is `412 precondition_failed`.
  - Audited as `migration_plan.reschedule`.
- `GET /v1/migration-plans/split?source_id&by=bytes|count&waves=N` → the source's next-level prefixes with their counts, packed into N waves of about the same size. Objects directly under the source prefix (`loose`) move only with the whole prefix.
  - Prefixes are folder names, so a signed-in caller sees only the folders they could read in a listing (§3.2). The rest are counted in `hidden`, never named, and left out of the waves.
  - An app reads at dataset level, once its client's owner can see the dataset (`403 owner_lacks_access`, §5.21).

## 6. Mapping to the catalog

| API | Catalog |
|---|---|
| `Entry.state` | `file.state` (`landing`, `uploading`, `resident`, `stub`, `recalling`, `remote`) |
| `Entry.tier` | the content version's primary `location`: `logical_tier`, `storage_class`, archived (`remote_tier = 'archive'` and no live restore), `explicit_tier_set` |
| `Entry.archive_at`, `next_action` | `file.archive_at`, `next_action`, `next_action_at` (set by the scheduler) |
| `Entry.move_hold` | computed: an active pin to another tier than the file's, not to Archive and not from it, another file's primary copy on the same blob, and the worker's report in the last day that a file on this blob couldn't get its own copy (decision 66, `tiering._wait_a_day`). `until` is a day after that report. While set, `next_action` and `next_action_at` are null |
| `Entry.read_back_failed_at` | computed: the primary copy is unverified, and the audit log has a `location.verify` deny for it (`web/entries.py`). The file stays on the file server and won't become a stub. It clears once the copy is verified. |
| `Entry.pin` | the nearest active `tier_pin` on the file or an ancestor, with `inherited_from` |
| `Entry.holds` | active `hold` rows on the file or an ancestor |
| `Recall.state` | `recall.state`; `copying` is shown as step 2 |
| `Recall.steps[].at` | `recall_state_log` (0010) |
| `Share` | `share` and its generated `trust_level` |
| `PolicyVersion` | `policy` |
| `Proposal` | `policy_proposal` |
| `VersionRestore`, `Download`, `PriceTable` | `version_restore`, `download_request`, `price_table` (0010) |

**Migration 0010** (Portal API track):
- `web_session`: `session_sha256` (PK), `identity_id` (NULL for cloud-only users), `entra_object_id`, `user_sid`, `display_name`, `upn`, `roles text[]`, `group_sids catalog.sid[]`, `groups_source` (`token`, `graph`, `winbind`, `pending` while an overage is resolved, `none`), `csrf_token`, `auth_time`, `created_at`, `last_seen_at`, `idle_expires_at`, `expires_at`.
- `oidc_login`: `state` (PK), `nonce`, `code_verifier`, `return_to` (checked to be a same-origin path), `reauth`, `created_at`, `expires_at`.
- `identity.groups_source`.
- `recall_state_log`: `recall_id`, `state`, `at`. A trigger writes it on insert and on every `recall.state` change.
- `version_restore`, `download_request` (the URL only while `ready`), `price_table` (per backend kind and region; `''` = all), as in §5.
- `policy.note`: the approver's note.
- `catalog.file_ancestors(file_id)`: a file and its ancestors, for access checks and paths.
- PGMQ queues `portal` and `identity_resolve`, with `_dlq`.

**Migration 0012** (Portal API track): `version_restore.target` becomes `overwrite` | `restored` | `restored_timestamped` (decision 34; old rows mapped), `version_restore.copy_*` records the copied blob so a retried restore resumes, and `policy_proposal.share_overrides` holds a proposal's "what users see" answers until approval.

Expired sessions, logins, downloads and restores are swept hourly by a `portal_jobs` sweep.

## 7. Errors

RFC 9457 problem details, in the same shape and with the same codes as `control-api.md` §9 (`api/problems.py`):

```json
{"type": "urn:omnifile:error:policy_invalid", "title": "Policy invalid", "status": 422,
 "code": "policy_invalid", "detail": "…", "retryable": false, "retry_after_s": null,
 "request_id": "…",
 "checks": [{"severity": "error", "code": "days_not_increasing", "message": "…", "field": "cold_day"}]}
```

Extension members:
- `errors` — `[{"field", "message"}]` for `invalid_request`;
- `checks` — for `policy_invalid`;
- `max_age_s` — for `reauth_required`.

Codes from the control API reused here: `invalid_request`, `not_found`, `version_conflict`, `worm_violation`, `request_in_progress`, `policy_not_approved`, `layout_conflict`, `explicit_tier_set`, `beyond_rewind_depth`, `cursor_expired`, `idempotency_key_reused`, `throttled`, `actor_required`, `internal`, `unavailable`.

New codes (added to `api/problems.py` by the coordinator before tracks start):

| HTTP | `code` | Retry? | Meaning |
|---|---|---|---|
| 401 | `unauthenticated` | no | No session, an expired session, or a bad or missing bearer token |
| 401 | `reauth_required` | no | Sign in again within `max_age_s` (§2.4) |
| 403 | `role_required` | no | The Entra app role is missing; `detail` names it |
| 403 | `forbidden` | no | The item is readable, but the share role or ownership is missing |
| 403 | `csrf_failed` | no | `X-CSRF-Token` or `Origin` is wrong |
| 403 | `self_approval` | no | The proposer can't approve their own proposal |
| 409 | `proposal_not_pending` | no | The proposal was already decided or withdrawn |
| 409 | `recall_not_needed` | no | The file is online or resident |
| 409 | `version_not_restorable` | no | A tombstone or pruned version |
| 409 | `worm_window_closed` | no | WORM cancel after the lock |
| 409 | `source_conflict` | no | The catalog's state refuses the source: an overlapping prefix, a dataset migrating from another backend, a path through a file, or a retry of a source that isn't `failed` |
| 409 | `migration_conflict` | no | The dataset isn't migrating, its source is still seeding, the prefix overlaps another location, or the migration's state refuses the action (§5.17) |
| 409 | `edge_enrolled` | no | A new enrolment bundle was asked for an edge that has already enrolled (or was revoked) |
| 409 | `dataset_shared` | no | A share was asked for a folder that is already shared |
| 403 | `client_not_registered` | no | A token's application is not registered in Settings, API clients (§2.2) |
| 403 | `client_disabled` | no | The API client is disabled or revoked |
| 403 | `owner_lacks_access` | no | The API client's owner can't see the dataset the proposal targets (§5.21), or the person named as an annex's owner is not in its Owners group (§5.23) |
| 409 | `share_in_use` | no | A root share was to be removed while shares of the dataset's folders need its folder |
| 409 | `dataset_annexed` | no | A share was asked for a dataset that holds an annex's copies (§5.23) |
| 409 | `file_present` | no | A purge was asked for an annexed file still on the NAS (§5.23) |
| 409 | `copy_not_refused` | no | A stop or acknowledgement of an annexed file whose copy no read-back refused (§5.23) |
| 422 | `owner_required` | no | An app created an annex without naming the person who owns it (§5.23) |
| 422 | `mode_not_available` | no | An annex mode later phases bring (§5.23) |
| 422 | `rules_check_failed` | no | An annex rule set fails a check that blocks saving (`rule_checks`: `{rule, check, message}`; §5.23) |
| 403 | `target_not_allowed` | no | The annex's return rules don't allow that target for the requester, the requester can't add a file there, or `on_conflict` is harsher than the rule's (§5.25) |
| 422 | `requester_required` | no | An API client asked for return targets or a placement without naming the person it is for (§5.25) |
| 409 | `placement_not_cancellable` | no | The placement ended, or has its `go`, so the file may already be under its name (§5.25) |
| 409 | `placement_not_abandonable` | no | The placement has no `go` yet (cancel it), ended, or the edge reported in the last 24 hours (§5.25) |
| 412 | `precondition_failed` | after a re-read | Stale `If-Match` |
| 422 | `confirmation_mismatch` | no | The typed share name differs |
| 422 | `policy_invalid` | no | The preview has error checks |
| 422 | `config_invalid` | no | An inline backend's config has a field a partner may not set, or names a bucket no admin configured; `detail` names the field (§5.10) |
| 422 | `batch_too_large` | no | A recall batch names more than 100 paths or holds more than 1,000,000 files (§5.19) |
| 422 | `dependency_failed` | no | A step needs an earlier one first: Connect Azure's grants before discovery, a Storage Mover grant before deploying (§5.20) |
| 428 | `precondition_required` | no | `If-Match` is missing |
| 503 | `groups_pending` | yes | The user's AD groups are being resolved (§3.3) |

Teams codes (`teams.md` §4.4): `intent_invalid`, `intent_mismatch`, `not_intended_recipient`, `intent_already_used`, `intent_expired`, `action_not_allowed`.

## 8. Check codes

`Check` = `{severity: error | warning | info, code, message, field, share_id, fee}`. Messages are plain language, in the backend's own terms, from `capabilities()`. The SPA shows the message and uses `code` only to place it.

| Code | Severity | From |
|---|---|---|
| `invalid_body` | error | A field of the wrong type (`PolicyError`) |
| `archive_day_required` | error | Both modes |
| `days_not_increasing` | error | `warm_day < cold_day < archive_day` |
| `smart_has_no_warm_cold` | error | Smart with warm or cold days |
| `smart_on_worm` | error | `model.problems` |
| `smart_unavailable` | error | `model.problems` |
| `smart_archive_needs_two_accounts` | error | `catalog.policy_layout_problem` |
| `leaves_hot_under_smart` | warning | `model.problems` |
| `minimum_duration` | warning | `model.problems`, with `fee` per TB |
| `archive_recall_time` | warning | `model.problems` |
| `smart_one_way` | info | A tier pin or explicit tier leaves Smart for good |
| `smart_clock_written` | info | Smart counts the archive day from the write |
| `archive_copy_two_accounts` | info | On the archive day the file is copied to the archive account, read back and then archived (M3) |
| `worm_tiers_in_place` | info | WORM shares tier in place; no Smart |
| `nobody_told` | warning | `notify.requester` and `notify.owners` are false, and there are no watchers |
| `no_email_fallback` | info | `notify.email_fallback` is false |
| `wait_without_promise` | warning | A share is `wait`, and files over the no-promise size get no time |
| `auto_threshold_below_eta` | warning | A share is `auto` with a threshold below the fastest offered restore, so archived reads always fail fast |
| `kerberos_throughput` | info | A share uses `krb5` (about −53% IOPS) or `krb5p` (about −77%) on NFS |
| `host_trusted` | warning | A share uses `sec=sys`: the uid is whatever the client says |
| `host_trusted_restricted` | error | Host-trusted while folders carry restricted groups, without the recorded override |
| `pin_early_deletion` | warning | A pin moves a file before its tier's minimum days; `fee` |
| `pin_own_copy` | info | The file shares its blob. Unless the other file wants this tier too, it gets its own copy (decision 66): no early deletion, and `fee` is one read of its tier. `monthly_cost_change` is the whole new blob |
| `pin_leaves_smart` | warning | A pin on a Smart blob |
| `pin_recall_time` | warning | A warmer pin on an archived file starts a recall; ETA or no promise |
| `pin_past_archive_date` | info | A warmer pin ends at the archive date unless `override_archive` |
| `price_missing` | warning | The price table has no price for a native class the policy uses (for example any S3 dataset until `policy/prices/s3.json` exists); the cost leaves it out |

## 9. Partner callers in M4

- **Built in M4:** `POST /v1/policies/proposals` and its reads (`Policy.Propose`); `POST /v1/policies/preview` (`Policy.Propose`); `GET /v1/datasets/{id}/policies` (`Archive.Read`); `POST /v1/recalls` with `file_id` (`Recall.Request`).
- `ARCHITECTURE.md` §6's "recall paths or datasets by a due date" arrives in M5, as more request shapes on the same route.

## 10. Endpoint index

`S` = session (cookie + CSRF on unsafe methods); `B` = bearer app token. A test checks this table against `portal-api.yaml`.

| Method and path | Auth | Who |
|---|---|---|
| `GET /auth/login` | none | anyone |
| `GET /auth/callback` | none | anyone (with a valid state) |
| `GET /auth/session` | S | signed in |
| `POST /auth/logout` | S | signed in |
| `GET /v1/me` | S | signed in |
| `GET /v1/directory/groups` | S | ArchiveAdmin, share Owners |
| `GET /v1/shares` | S | any role |
| `GET /v1/shares/{share_id}` | S | any role (§5.2) |
| `PATCH /v1/shares/{share_id}/settings` | S | per field (§5.6) |
| `POST /v1/shares/{share_id}/settings/preview` | S | ArchiveAdmin, Owners |
| `PUT /v1/shares/{share_id}/access` | S | ArchiveAdmin; Owners for Writers and Readers |
| `POST /v1/shares/{share_id}/worm/enable` | S | ArchiveAdmin, re-auth |
| `POST /v1/shares/{share_id}/worm/cancel` | S | ArchiveAdmin, re-auth |
| `POST /v1/shares/{share_id}/worm/extend` | S | ArchiveAdmin |
| `GET /v1/dirs/{dir_id}/children` | S | read on the folder |
| `GET /v1/files/{file_id}` | S | read |
| `GET /v1/files/{file_id}/versions` | S | read |
| `GET /v1/files/{file_id}/recall-options` | S | read |
| `POST /v1/files/{file_id}/versions/{version_id}/restore` | S | Owner or Writer, and read on the version |
| `POST /v1/files/{file_id}/versions/{version_id}/downloads` | S | read on the version |
| `GET /v1/version-restores/{restore_id}` | S | requester, ArchiveAdmin |
| `GET /v1/downloads/{download_id}` | S | requester |
| `POST /v1/recalls` | S, B | read (S); `Recall.Request` (B) |
| `GET /v1/recalls` | S | per scope (§5.5) |
| `GET /v1/recalls/{recall_id}` | S | requester or read |
| `POST /v1/pins/preview` | S | Owners, DataSteward |
| `POST /v1/pins` | S | Owners, DataSteward |
| `GET /v1/pins` | S | Owners, DataSteward, ArchiveAdmin |
| `POST /v1/pins/{pin_id}/release` | S | Owners, DataSteward |
| `POST /v1/holds` | S | keep_online: Owners, DataSteward; legal: ArchiveAdmin, re-auth |
| `GET /v1/holds` | S | Owners, DataSteward, ArchiveAdmin |
| `POST /v1/holds/{hold_id}/release` | S | as §5.6 |
| `GET /v1/datasets` | S | ArchiveAdmin, DataSteward |
| `GET /v1/datasets/{dataset_id}/policy` | S, B | ArchiveAdmin, DataSteward, Owners; `Archive.Read` |
| `GET /v1/datasets/{dataset_id}/policies` | S, B | as above |
| `POST /v1/datasets/{dataset_id}/policies` | S | ArchiveAdmin |
| `POST /v1/policies/preview` | S, B | ArchiveAdmin, DataSteward; `Policy.Propose` |
| `POST /v1/policies/proposals` | S, B | ArchiveAdmin, DataSteward; `Policy.Propose` |
| `GET /v1/policies/proposals` | S, B | ArchiveAdmin, DataSteward; apps: their own |
| `GET /v1/policies/proposals/{proposal_id}` | S, B | as above |
| `POST /v1/policies/proposals/{proposal_id}/approve` | S | ArchiveAdmin, not the proposer |
| `POST /v1/policies/proposals/{proposal_id}/reject` | S | ArchiveAdmin |
| `POST /v1/policies/proposals/{proposal_id}/withdraw` | S, B | the proposer |
| `GET /v1/price-tables/{backend_kind}` | S | ArchiveAdmin; DataSteward unless prices are hidden from it (§5.24) |
| `PUT /v1/price-tables/{backend_kind}` | S | ArchiveAdmin |
| `DELETE /v1/price-tables/{backend_kind}` | S | ArchiveAdmin |
| `GET /v1/audit` | S | ArchiveAdmin; Owners with `share_id` |
| `POST /v1/sources` | S, B | ArchiveAdmin; `Archive.Adopt` |
| `POST /v1/adopt` | S, B | ArchiveAdmin; `Archive.Adopt` |
| `GET /v1/sources` | S, B | ArchiveAdmin; `Archive.Read`, `Archive.Adopt` |
| `GET /v1/sources/{source_id}` | S, B | as above |
| `POST /v1/sources/{source_id}/retry` | S, B | ArchiveAdmin; `Archive.Adopt` |
| `GET /v1/source-backends` | S | ArchiveAdmin |
| `GET /v1/files` | S, B | ArchiveAdmin; `Archive.Read` |
| `GET /v1/restores` | S, B | ArchiveAdmin; `Archive.Read` |
| `GET /v1/events` | S, B | ArchiveAdmin; `Archive.Read` |
| `GET /v1/usage` | S, B | ArchiveAdmin; `Archive.Read` |
| `POST /v1/locations` | S, B | ArchiveAdmin; `Archive.Adopt` |
| `GET /v1/locations` | S, B | ArchiveAdmin; `Archive.Read`, `Archive.Adopt` |
| `GET /v1/locations/{migration_id}` | S, B | as above |
| `POST /v1/migrations` | S, B | ArchiveAdmin; `Archive.Adopt` |
| `GET /v1/migrations` | S, B | ArchiveAdmin; `Archive.Read`, `Archive.Adopt` |
| `GET /v1/migrations/{migration_id}` | S, B | as above |
| `POST /v1/migrations/{migration_id}/cutover` | S, B | ArchiveAdmin; `Archive.Adopt`, the app that started it |
| `POST /v1/migrations/{migration_id}/cancel` | S, B | as above |
| `POST /v1/migrations/{migration_id}/retry` | S, B | as above |
| `POST /v1/migrations/{migration_id}/runs` | B | `Archive.Adopt`, the app that started it |
| `POST /v1/migrations/{migration_id}/exclude` | S | ArchiveAdmin, re-auth |
| `GET /v1/migrations/{migration_id}/tombstones` | S, B | ArchiveAdmin with an AD identity; `Archive.Read` or `Archive.Adopt`, the app that started it; `Archive.Adopt`, for one started in the portal |
| `GET /v1/migration-console` | S | ArchiveAdmin |
| `GET /v1/migration-agents` | S | ArchiveAdmin |
| `GET /v1/migrations/{migration_id}/agents` | S | ArchiveAdmin |
| `GET /v1/migrations/{migration_id}/stuck-copies` | S | ArchiveAdmin; rows need an AD identity |
| `POST /v1/migrations/{migration_id}/rerun` | S | ArchiveAdmin |
| `POST /v1/migrations/{migration_id}/restore-again` | S | ArchiveAdmin |
| `POST /v1/migration-plans` | S, B | ArchiveAdmin (approved); `Archive.Adopt` (a proposal) |
| `GET /v1/migration-plans` | S, B | ArchiveAdmin; `Archive.Read` or `Archive.Adopt`, its own |
| `GET /v1/migration-plans/split` | S, B | ArchiveAdmin; `Archive.Read` or `Archive.Adopt` |
| `GET /v1/migration-plans/{plan_id}` | S, B | as above, an app its own |
| `POST /v1/migration-plans/{plan_id}/approve` | S | ArchiveAdmin, not the proposer |
| `POST /v1/migration-plans/{plan_id}/reject` | S | ArchiveAdmin, not the proposer |
| `POST /v1/migration-plans/{plan_id}/cancel` | S | ArchiveAdmin |
| `PATCH /v1/migration-plans/{plan_id}/waves/{seq}` | S | ArchiveAdmin |
| `POST /v1/migration-plans/{plan_id}/waves/{seq}/cutover` | S | ArchiveAdmin |
| `POST /v1/migration-plans/{plan_id}/waves/{seq}/retry` | S | ArchiveAdmin |
| `POST /v1/migration-plans/{plan_id}/waves/{seq}/cancel` | S | ArchiveAdmin |
| `POST /v1/recall-batches` | B | `Recall.Request` |
| `GET /v1/recall-batches/{batch_id}` | B | `Recall.Request`, the app that made it |
| `GET /v1/files/{file_id}/journey` | S | read (§5.22) |
| `GET /v1/shares/{share_id}/map` | S | who can see the share; whole-share counts for ArchiveAdmin and DataSteward (§5.22) |
| `GET /v1/shares/{share_id}/map/files` | S | who can see the share; readable files only |
| `GET /v1/activity` | S, B | ArchiveAdmin; `Archive.Read` |
| `GET /v1/activity/live` | S, B | ArchiveAdmin; `Archive.Read`; names only where readable |
| `GET /v1/edges` | S, B | ArchiveAdmin; `Archive.Read` |
| `GET /v1/edges/appliance` | S | ArchiveAdmin |
| `POST /v1/edges` | S | ArchiveAdmin |
| `POST /v1/edges/{edge_id}/bundle` | S | ArchiveAdmin |
| `POST /v1/edges/{edge_id}/revoke` | S | ArchiveAdmin, re-auth |
| `GET /v1/backends` | S | ArchiveAdmin, DataSteward |
| `POST /v1/datasets` | S | ArchiveAdmin |
| `POST /v1/shares` | S | ArchiveAdmin |
| `DELETE /v1/shares/{share_id}` | S | ArchiveAdmin |
| `GET /v1/settings/storage` | S | ArchiveAdmin |
| `POST /v1/settings/storage` | S | ArchiveAdmin, re-auth |
| `PUT /v1/settings/storage/{backend_id}` | S | ArchiveAdmin, re-auth |
| `GET /v1/settings/storage/{backend_id}/checks` | S | ArchiveAdmin |
| `GET /v1/settings/aws` | S | ArchiveAdmin |
| `GET /v1/settings/aws/trust-policy` | S | ArchiveAdmin |
| `POST /v1/settings/aws/test` | S | ArchiveAdmin |
| `GET /v1/settings/identity` | S | ArchiveAdmin |
| `GET /v1/settings/identity/whoami` | S | ArchiveAdmin |
| `GET /v1/settings/teams` | S | ArchiveAdmin |
| `GET /v1/settings/teams/package` | S | ArchiveAdmin |
| `POST /v1/settings/teams/test-card` | S | ArchiveAdmin |
| `GET /v1/settings/email` | S | ArchiveAdmin |
| `POST /v1/settings/email/test` | S | ArchiveAdmin |
| `GET /v1/settings/tests/{test_id}` | S | ArchiveAdmin, the asker |
| `GET /v1/settings/storage-mover` | S | ArchiveAdmin |
| `POST /v1/settings/storage-mover/deploy` | S | ArchiveAdmin |
| `GET /v1/settings/tasks/{task_id}` | S | ArchiveAdmin |
| `GET /v1/settings/azure` | S | ArchiveAdmin |
| `POST /v1/settings/azure/sign-in` | S | ArchiveAdmin |
| `GET /auth/azure/callback` | none | the admin who started it (cookie, state, nonce, oid) |
| `PUT /v1/settings/azure/choice` | S | ArchiveAdmin |
| `POST /v1/settings/azure/discover` | S | ArchiveAdmin |
| `POST /v1/settings/azure/confirm` | S | ArchiveAdmin |
| `GET /v1/settings/aws/connect` | S | ArchiveAdmin |
| `GET /v1/settings/aws/connect/template` | S | ArchiveAdmin |
| `POST /v1/settings/aws/connect/fetch` | S | ArchiveAdmin |
| `POST /v1/settings/aws/connect/outputs` | S | ArchiveAdmin |
| `POST /v1/settings/aws/connect/confirm` | S | ArchiveAdmin |
| `POST /v1/source-scans` | S | ArchiveAdmin |
| `GET /v1/source-scans/{scan_id}` | S | ArchiveAdmin |
| `GET /v1/settings/alerts` | S | ArchiveAdmin |
| `GET /v1/settings/prices` | S | ArchiveAdmin |
| `PUT /v1/settings/prices` | S | ArchiveAdmin, audited |
| `GET /v1/settings/people` | S | ArchiveAdmin |
| `GET /v1/settings/api-clients` | S | ArchiveAdmin |
| `POST /v1/settings/api-clients` | S | ArchiveAdmin, re-auth |
| `GET /v1/settings/api-clients/{api_client_id}` | S | ArchiveAdmin |
| `PATCH /v1/settings/api-clients/{api_client_id}` | S | ArchiveAdmin; re-auth unless only disabling |
| `POST /v1/settings/api-clients/{api_client_id}/revoke` | S | ArchiveAdmin |
| `POST /v1/settings/api-clients/{api_client_id}/keys` | S | ArchiveAdmin, re-auth |
| `POST /v1/settings/api-clients/{api_client_id}/keys/{key_id}/rotate` | S | ArchiveAdmin, re-auth |
| `POST /v1/settings/api-clients/{api_client_id}/keys/{key_id}/revoke` | S | ArchiveAdmin |
| `GET /v1/filers` | S, B | ArchiveAdmin, DataSteward; apps `Archive.Read` or `Archive.Adopt` (§5.23) |
| `POST /v1/filers` | S | ArchiveAdmin |
| `GET /v1/filers/{filer_id}` | S, B | as `GET /v1/filers` |
| `GET /v1/filers/{filer_id}/health` | S, B | as `GET /v1/filers` |
| `GET /v1/annexes` | S, B | any role; apps `Archive.Read` or `Archive.Adopt` |
| `POST /v1/annexes` | S, B | ArchiveAdmin, DataSteward in the Owners group; apps `Archive.Adopt` naming an owner |
| `GET /v1/annexes/{annex_id}` | S, B | ArchiveAdmin, DataSteward, the annex's Owners; apps |
| `PATCH /v1/annexes/{annex_id}` | S, B | the annex's Owners, ArchiveAdmin; apps `Archive.Adopt` whose client owner sees the dataset |
| `GET /v1/annexes/{annex_id}/rulesets` | S, B | as `GET /v1/annexes/{annex_id}` |
| `POST /v1/annexes/{annex_id}/rulesets` | S, B | the annex's Owners, ArchiveAdmin; apps `Archive.Adopt` naming an owner |
| `GET /v1/annexes/{annex_id}/rulesets/{version}` | S, B | as `GET /v1/annexes/{annex_id}` |
| `POST /v1/annexes/{annex_id}/rulesets/{version}/approve` | S | ArchiveAdmin for a removal, else the annex's Owners; never the proposal's own person |
| `POST /v1/annexes/{annex_id}/rulesets/{version}/reject` | S | the annex's Owners, ArchiveAdmin |
| `POST /v1/annexes/{annex_id}/dry-runs` | S, B | the annex's Owners, DataSteward, ArchiveAdmin; apps |
| `GET /v1/annexes/{annex_id}/dry-runs/{run_id}` | S, B | as `GET /v1/annexes/{annex_id}` |
| `POST /v1/annexes/{annex_id}/return-preview` | S, B | ArchiveAdmin; apps |
| `GET /v1/files/{file_id}/return-targets` | S, B | anyone the file's NAS ACL lets read it; apps `Recall.Request` naming the person |
| `POST /v1/placements` | S, B | as `GET /v1/files/{file_id}/return-targets` |
| `GET /v1/placements/{placement_id}` | S, B | the requester, the API client that asked, ArchiveAdmin |
| `POST /v1/placements/{placement_id}/cancel` | S | the requester, ArchiveAdmin |
| `POST /v1/placements/{placement_id}/abandon` | S | ArchiveAdmin |
| `PATCH /v1/filers/{filer_id}` | S | ArchiveAdmin |
| `POST /v1/annexes/{annex_id}/files/{file_id}/clear-breaker` | S | ArchiveAdmin, the annex's Owners |
| `POST /v1/annexes/{annex_id}/files/{file_id}/copy-stop` | S | ArchiveAdmin, signed in within 10 minutes |
| `POST /v1/annexes/{annex_id}/files/{file_id}/copy-resume` | S | ArchiveAdmin, signed in within 10 minutes |
| `POST /v1/annexes/{annex_id}/files/{file_id}/copy-refused-ack` | S | ArchiveAdmin, signed in within 10 minutes |
| `POST /v1/annexes/{annex_id}/removals/confirm` | S | ArchiveAdmin, recent sign-in; never the rules' proposer or anyone answerable for their app |
| `GET /v1/annexes/{annex_id}/removals` | S, B | as `GET /v1/annexes/{annex_id}`, filtered by the files' NAS ACLs |
| `GET /v1/annexes/{annex_id}/events` | S, B | as `GET /v1/annexes/{annex_id}`, filtered by the files' NAS ACLs |
| `GET /v1/annexes/{annex_id}/files` | S, B | as `GET /v1/annexes/{annex_id}`, filtered by the files' NAS ACLs |
| `GET /v1/annexes/{annex_id}/timeline` | S, B | as `GET /v1/annexes/{annex_id}` |
| `GET /v1/annexes/{annex_id}/problems` | S, B | as `GET /v1/annexes/{annex_id}` |
| `POST /v1/annexes/{annex_id}/files/{file_id}/purge` | S | ArchiveAdmin, recent sign-in |
| `GET /v1/settings/annex-defaults` | S | ArchiveAdmin, DataSteward |
| `PUT /v1/settings/annex-defaults` | S | ArchiveAdmin |
| `GET /healthz` | none | liveness |

## 11. Reserved for M5 (partner API)

`POST /v1/adopt`, `POST /v1/sources`, `POST /v1/locations`, `GET /v1/files?path=`, `GET /v1/usage`, `GET /v1/restores`, `GET /v1/events?after=` (`ARCHITECTURE.md` §6). They use the same auth (§2.2), errors, pagination and idempotency. Until each is built it isn't routed and answers `404 not_found`. **Built:** `sources` and `adopt` (§5.10, with the draft's inline `backend`, allow-listed), `files?path=`, `restores`, `events` and `usage` (§5.11, with the draft's typed `migration.*` events), `locations` (§5.12), and beyond the draft: the migration lifecycle (§5.17), the cutover report (§5.18) and recall batches (§5.19), ported from line B.

**Intended shapes (M5, draft).** Written backend-neutrally now, so M5 doesn't build them for S3 and Azure only. No routes, no YAML yet; M5 settles the details.

- `POST /v1/sources` `{backend_id | backend: {kind, config}, prefix, inventory?, groups: {owners, writers?, readers?}}` registers data that already lives in a backend as a migration source. `kind` is any backend kind (`s3`, `azure_blob`, and later others such as an on-premises NAS); `config` holds names, never secrets. `inventory` names a native inventory or listing to seed the catalog from (S3 Inventory, Azure Blob Inventory, a NAS snapshot listing); absent: list the prefix.
- `POST /v1/locations` `{dataset_id, prefix, to_backend_id, mode: serve | hand_off}` moves a prefix of a dataset to another backend:
  - `serve`: the new backend becomes where the bytes live, behind the same OmniFile share (as S3 → Azure today);
  - `hand_off`: copy, verify (decision 24's full check), and then the new backend serves users directly. The dataset ends `handed_off` (`migration-namespace.md`, decision 35).
- `GET /v1/events?after=` includes dataset lifecycle events next to file events: `migration.started`, `migration.verified`, `migration.cut_over` and `migration.handed_off`, each with `dataset_id`, `prefix` (if partial), `from_backend_id`, `to_backend_id`, `mode` and `at`. The `kind` field is an open enum (§1).
