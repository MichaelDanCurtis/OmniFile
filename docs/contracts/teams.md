# Service ⇄ relay ⇄ Teams contract

**Status: M4, built against fakes (2026-10-06).** The relay side is built (`relay/`), and the service side now matches it: `api/teams.py`, `notify/`, `worker/notify_jobs.py`, migration `0011_notify.sql`. Real-tenant checks: `docs/milestones/real-tenant-checks.md`. When the relay's code and this file disagree, the relay's code wins until this file is corrected; it is the deployed public endpoint.

Parties:
- **Teams** (Bot Framework, Bot Connector service).
- **The relay** (`relay/`): the only public endpoint. It checks the Bot Framework JWT, the tenant, the signed intent and the clicker, then forwards. It holds no bot credentials and no signing key.
- **`api`**: private, mutual TLS. It serves `POST /internal/v1/teams/intents`.
- **The worker**: sends and updates cards, resolves identities, sends email. It holds the bot credential and the intent signing key.
- **Microsoft Graph**: identity lookup, Teams app install, `sendMail`.

```
worker ──Graph──► install app for user, find chat
worker ──Bot Connector──► send card (buttons carry signed intents)
user clicks ─► Teams ─► relay POST /api/messages ─mTLS─► api POST /internal/v1/teams/intents
api: verify, consume, act, enqueue notify (+6 s) ─► reply card (no Execute buttons) ─► relay ─► Teams
worker (notify) ──Bot Connector PUT──► full card with fresh intents
```

## 1. Identifiers and formats

- Entra objectIds and tenant ids are lowercase GUIDs.
- Catalog ids are decimal strings, as in the control API.
- Times are RFC 3339 UTC.
- The relay compares `sub` and `tid` lowercased, so the service always mints them lowercase.

## 2. The signed intent (copied from `relay/src/omnifile_relay/intent.py`)

One token per button, minted by the worker when it renders a card. It goes in the button's `Action.Execute` `data` under the key **`omnifile_intent`**.

A compact JWS, signed with **Ed25519**:

```
header  {"alg": "EdDSA", "typ": "omnifile-intent+jwt", "kid": "<key id>"}
claims  jti  intent id: a UUID (lowercase, hyphenated), random, single use
        act  "recall" | "keep_online" | "track"
        fid  catalog file id, decimal string
        sub  the recipient's Entra objectId, lowercase GUID
        tid  the Entra tenant id, lowercase GUID
        iat  issued, integer seconds
        exp  expiry, integer seconds
        iss  "omnifile-service"
        aud  "omnifile-teams-relay"
```

Rules the relay enforces, so the service must meet them:
- Token at most **2,048 bytes** (about 560 in practice).
- `jti` and `fid` match `^[A-Za-z0-9_.:-]{1,128}$`. `jti` must also be a UUID, because `catalog.teams_intent.intent_id` is `uuid`.
- `exp - iat` is more than 0 and at most **14 days**. The service uses **7 days**; each card update mints fresh intents.
- `iat` is not in the future, and expiry has 60 s leeway.
- The button's `verb` equals `act`.
- `iat` and `exp` are **wall-clock** time, even when the worker runs on the dev virtual clock: the relay and `api` check them against their own wall clocks (`api` too, though its catalog work follows the virtual clock in dev).

Keys:
- `kid` is a short label, for example `intent-2026-10`.
- The worker holds the private key: `OMNIFILE_TEAMS_INTENT_SIGNING_KEY` (path to a PKCS#8 PEM, mounted from a Key Vault **secret**) and `OMNIFILE_TEAMS_INTENT_KID`. Azure Key Vault keys can't be Ed25519, hence a secret.
- `api` and the relay hold public keys only, as JSON `{kid: key}`. Each key is a PEM SubjectPublicKeyInfo, or base64url of the raw 32 bytes. The variables are `OMNIFILE_TEAMS_INTENT_PUBLIC_KEYS` in `api` and `OMNIFILE_RELAY_INTENT_PUBLIC_KEYS` in the relay, same format.
- **Rotation:** add the new public key to both, deploy, switch the worker's `kid`, and drop the old key after 14 days.
- The key signs nothing else. `typ` and `aud` stop other tokens being accepted.

Each minted intent is a `catalog.teams_intent` row: intent id, notification, action, file id, **version id**, recipient identity, issued and expiry, and the `card_version` that carried it. The token carries `fid` only; the row says which content version the card was about. Rows are written only after the card was sent or updated successfully, so a card that never reached Teams leaves no live intents.

## 3. Cards

Adaptive Card **1.4** (the Universal Actions minimum). Every card is rendered by `notify.cards.render(state, audience, intents)`, a pure function. The worker passes intents. `api` passes `None`, which drops every `Action.Execute`.

### 3.1 Audiences

| Audience | Who | Gets a card when |
|---|---|---|
| `requester` | A `recall_requester` with an identity and an Entra objectId: read over NFS, SMB or locally, or asked in the portal, Teams or API | The recall is created or joined, and at every state change |
| `owner` | Members of the share's Owners group, if the policy's `notify.owners` is true | Once, at `requested`. Later updates only if they press Track |
| `watcher` | Members of `notify.watchers_group_sid`, if set | As `owner` |

- The policy body's `notify` object: `{"requester": true, "owners": false, "watchers_group_sid": null, "email_fallback": true}`. These are the defaults when it is missing.
- **Nobody gets a card for a file they can't read.** The worker checks `catalog.acl_can_read` with the recipient's groups before every send and update. A recipient who lost access gets a last card ("You no longer have access to this file") and nothing after.
- **Group expansion** (owner and watcher audiences) uses Graph `transitiveMembers`, capped at 50 recipients per recall. Above the cap, only the requester is told, and the audit row says so.

### 3.2 States and buttons

The recall's catalog state drives the card. `copying` shows as step 2, like `rehydrating`.

| State | Title | Body | Requester buttons | Owner/watcher buttons |
|---|---|---|---|---|
| `requested` | Recalling *name* | Step 1 of 4, Requested. ETA, or "No time is promised for files over 10 GB". Share and path. | Keep online · Open in portal | Track · Open in portal |
| `rehydrating`, `copying` | Recalling *name* | Step 2 of 4, Coming back from Archive. ETA. | Keep online · Open in portal | Track (unless tracking) · Open in portal |
| `downloading` | Recalling *name* | Step 3 of 4, Copying to the file server. | Keep online · Open in portal | Track (unless tracking) |
| `available` | *name* is ready | Step 4 of 4, Ready on *share*. Path. "Kept online until *date*" when a hold exists. | Keep online | (none) |
| `failed` | Couldn't recall *name* | A generic reason, never provider or edge error text: "The storage service couldn't restore it", or, for a fill the file server could not finish (control API §4.4), that it ran out of disk space, that its copy didn't match the stored file, or that it couldn't finish copying; for a recall the service failed because its edge went quiet, that the file server stopped responding, or, because its edge ran no fill, that the file server isn't copying it. Then "Open the file again to try again." | Recall · Open in portal | Recall |
| `cancelled` | Recall cancelled | Who cancelled: "an administrator" or "you". | Recall | (none) |
| `superseded` | *name* was replaced, or *name* was deleted | "This file was replaced (or deleted) while it was being recalled, so the recall has stopped." Then "Replaced by *who*." or "Deleted by *who*." when the catalog knows who and may say (below). Never "failed", and nothing to try again. | Open in portal | (none) |

- **Who replaced or deleted it** (a `superseded` card) is the display name of the actor of the version that ended the recall. It is shown only when the edge proved who it was (Kerberos NFS, or SMB on a proven share; an NFS `sec=sys` uid names no one), and only on a card the recipient gets at all: someone who can no longer read the file gets the "no longer have access" card, which says nothing of the file. An upload's content version records no actor today, so in practice a delete names its person and a replace does not.
- **Keep online** is left off any card while the file has an active `keep_online` hold (the card shows "Kept online until *date*" instead), and **Track** is left off once the recipient is tracking.
- **Open in portal** is `Action.OpenUrl` to `<portal>/recalls/<recall_id>`. It carries no intent. The portal is private, so it opens only on the client's network.
- **Fact set** on every card: share, path (each segment escaped; at most 200 characters, the middle elided), size, priority, requested at.
- **Identity note** when the requester's proof is `claimed`: "Opened over NFS from *client address*. NFS sec=sys identities are claimed by the client, not proven."
- **Size:** at most 20 KB of JSON (Teams caps cards at about 28 KB). Long paths are elided first.
- **No secrets, tokens or ids other than the recall id** in visible text. Intents are only in `data`.

### 3.3 Button actions

| `act` | Effect (in one transaction in `api`) | Allowed when |
|---|---|---|
| `recall` | Refused with `422 action_not_allowed` when no recall is needed: the edge holds that version whole, or the bytes are readable now (not archived, or a live restored copy). Otherwise, start or join a recall of the intent row's `version_id`, with the clicker as a requester (`via = teams`, `proof = proven`), priority `standard`. Sends `{"recall_id"}` on `restores` if new, and on `notify` (6 s delay). Audits `recall.request`. When the click was on an older recall's card (Recall on a `failed` or `cancelled` card), the clicker's notification for the new recall takes over that card's activity, and the old card's unused intents are revoked, so the same card follows the new recall. | The clicker can read the file. No active recall of that version, or joining one. |
| `keep_online` | `catalog.hold` of kind `keep_online` on that one file, `until = now + 14 days` (fixed by decision 33; not a setting), `set_by_identity_id` = the clicker, reason "Teams". An existing active hold by the same identity is extended, never shortened. Audited by the hold trigger. | The clicker can read the file. |
| `track` | `notification.tracking = true` for (recall, clicker, teams). | The clicker can read the file, and the recall is active. |

## 4. `POST /internal/v1/teams/intents` (in `api`)

### 4.1 Caller

- Mutual TLS. The client certificate chains to the OmniFile CA and carries SAN `URI:urn:omnifile:relay`.
- The principal is `relay`. It may call this path and nothing else; every other path answers `403 caller_not_allowed`.
- Its SHA-256 fingerprint is one of `OMNIFILE_TEAMS_RELAY_CERT_SHA256` (pinned, so a relay certificate is revoked by removing its pin). Only dev mode may leave the list empty, which accepts any CA-issued certificate with the relay SAN.
- Edge certificates are refused here with `403 caller_not_allowed`, and so are a missing, unreadable, expired or foreign certificate: this path never answers 401.
- Forwarded-certificate rules as for edges (`control-api.md` §2).

### 4.2 Request

Headers: `Idempotency-Key: <intent_id>` and `Content-Type: application/json`. A key that isn't the body's `intent_id` is `400 invalid_request`.

Body: schema `TeamsIntentForward` in `service/openapi/control-api.yaml` (no other fields). It is exactly what `relay/src/omnifile_relay/sink.py` `Forward.to_json()` sends:

```json
{"intent": "<compact JWS, as on the card>",
 "intent_id": "8f0e…", "action": "recall",
 "clicker_aad_object_id": "…", "tenant_id": "…",
 "activity_time": "2026-10-06T09:14:03.120Z", "received_at": "2026-10-06T09:14:03.410Z"}
```

### 4.3 Checks, in order

Each refusal is audited (`teams.intent.refused`, decision `deny`, with the reason) and answered as in 4.4.

1. Body schema. `activity_time` and `received_at` within 10 minutes of the service's wall clock (not the dev virtual clock: Teams and the relay stamp wall-clock times).
2. JWS: `alg` is `EdDSA`; `typ`, `kid` known; signature, `iss`, `aud` and the required claims as in §2; not expired (60 s leeway).
3. `intent_id` and `action` equal the JWS `jti` and `act`.
4. `tid` and `tenant_id` equal the configured tenant.
5. `clicker_aad_object_id` equals `sub`.
6. A `teams_intent` row exists for `jti`, with the same action, file and recipient (by `identity.entra_object_id`), not revoked.
7. Consume once: `UPDATE catalog.teams_intent SET used_at = now(), used_by_object_id = … WHERE intent_id = … AND used_at IS NULL AND revoked_at IS NULL`. No row means a repeat.
8. The clicker can still read the file: `catalog.acl_can_read` with the identity's SID and cached `group_sids` (decision 28). The groups must be younger than 1 h (`groups_resolved_at`, on the wall clock). A card stays clickable for the intent's 7 days, so older groups could let a user removed from a group still act. Older groups are not used: the click is answered `503 unavailable`, an `identity_resolve` message refreshes them, and the user clicks again. A file whose ACL the service doesn't know is not readable.
9. The action (§3.3), in the same transaction as step 7.

Steps 1-5 (and the 10-clicks-a-minute limit) run on every request, before the idempotency store, and their refusals are never stored: they depend on the clicker and the clock, so a stored reply could otherwise reach someone other than the recipient, or a stranger's refused click could block the recipient's button. Only steps 6-9 run inside the store.

A refusal at step 8 or 9 rolls back step 7: the intent stays unused, and the stored `403`/`422` reply answers a repeat for 24 h. More than 10 clicks per minute for one recipient (per `api` replica) is `429 throttled`, checked after step 5.

### 4.4 Reply

**Success:** `200`. Body (schema `TeamsIntentReply`):

```json
{"result": "queued" | "done",
 "recall_id": "123",
 "card": { "type": "AdaptiveCard", … },
 "message": "…"}
```

- `result` is `queued` for `recall`, and `done` for `keep_online` and `track`.
- `recall_id` is present for `recall`.
- `card` is the buttonless card for the new state (§5).
- `message` is unused in v1.
- The relay reads only `card` and `message` (`sink.parse_reply`); the rest is for logs and tests.

**Errors:** RFC 9457 problem details. The status codes are chosen so the relay reacts correctly (`sink.HttpsMtlsSink.submit`):

| HTTP | `code` | Relay treats it as | Cause |
|---|---|---|---|
| 400 | `invalid_request` | refused | Schema or time check (step 1) |
| 400 | `intent_mismatch` | refused | Step 3 |
| 403 | `intent_invalid` | refused | Bad signature, unknown `kid`, wrong `typ`/`iss`/`aud`/`tid`, unknown or revoked row (steps 2, 4, 6) |
| 403 | `not_intended_recipient` | refused | Step 5 |
| 403 | `forbidden` | refused | The clicker can't read the file (step 8) |
| 403 | `caller_not_allowed` | refused | Not the relay's certificate |
| 409 | `intent_already_used` | duplicate | Step 7 |
| 410 | `intent_expired` | refused | Expired (step 2) |
| 422 | `action_not_allowed` | refused | The action doesn't apply now: Track on a finished recall, or Recall when the file is online (portal: `409 recall_not_needed`; a 409 here would read as a duplicate) |
| 429 | `throttled` | transient | More than 10 clicks per minute for one recipient |
| 5xx | `unavailable` / `internal` | transient, reservation released | Catalog down, a bug, `api` has no Teams configuration (`OMNIFILE_TEAMS_INTENT_PUBLIC_KEYS`, `OMNIFILE_TENANT_ID`), or the clicker's groups are older than 1 h (step 8; a refresh is queued) |

- **Never 401 from this endpoint.** The relay treats 401 as transient: it would release its replay reservation and show "couldn't be reached".
- `Idempotency-Key` is the `intent_id` (principal `relay`). For this path the request hash covers the **`intent` field only**. A second relay replica forwarding the same click has a different `received_at`; it gets the stored first reply, not `422 idempotency_key_reused`, which the relay would show as a refusal. A different `intent` under the same key can't reach the store: its `jti` differs from the key, so step 3 refuses it (`400 intent_mismatch`).
- After the stored reply expires (24 h), a repeat reaches step 7 and gets `409 intent_already_used`.

## 5. Who answers the invoke, and the card afterwards

1. Teams waits 5 s for the invoke response. The relay gives `api` 3 s.
2. `api` answers. It holds no signing key, so its card has no `Action.Execute` buttons, only `Action.OpenUrl`.
3. The relay replies to Teams with `{"statusCode": 200, "type": "application/vnd.microsoft.card.adaptive", "value": <card>}`. Teams replaces the clicker's card with it.
4. In the transaction of step 9 (§4.3), `api` sends `{"recall_id", "reason": "click"}` on `notify` with a **6-second delay** (`pgmq.send(…, 6)`).
5. The worker renders the full card with fresh intents and updates the activity in place (§6.4). The delay keeps step 3's reply from landing after, and overwriting, the worker's update.
6. Old intents on that card are revoked: `teams_intent.revoked_at` is set for every unused intent of the notification's previous `card_version`.

**No Universal Actions `refresh`.** Cards carry no `refresh` property: the relay forwards only `trigger = manual` invokes. Every later change is a proactive update by the worker.

## 6. Delivery (worker)

### 6.1 Bot credential

- A single-tenant Azure Bot. The worker gets a Bot Connector token by client credentials: `POST https://login.microsoftonline.com/<tenant>/oauth2/v2.0/token`, scope `https://api.botframework.com/.default`.
- The client authenticates with the worker's managed identity, if the bot is a user-assigned managed identity bot. Otherwise it uses a certificate or secret from Key Vault. VERIFY which the client's tenant allows.
- Tokens are cached until 5 minutes before expiry.

### 6.2 Proactive install and conversation

Once per recipient, cached in `catalog.teams_conversation`:
1. `POST https://graph.microsoft.com/v1.0/users/<oid>/teamwork/installedApps` with `{"teamsApp@odata.bind": "https://graph.microsoft.com/v1.0/appCatalogs/teamsApps/<teams app id>"}`. `409` means already installed, which is fine.
2. `GET …/users/<oid>/teamwork/installedApps?$expand=teamsApp&$filter=teamsApp/id eq '<teams app id>'`, then `GET …/installedApps/<installation id>/chat`. The chat id is the conversation id.
3. The service URL is `OMNIFILE_TEAMS_SERVICE_URL`, default `https://smba.trafficmanager.net/teams/`. It is never taken from a click: the relay doesn't forward `serviceUrl`, by design. VERIFY the regional form for the client.

A `403` from Graph at step 1 (app blocked by tenant policy, risk R5), or a user with no Teams licence, stores the row with `state = 'blocked'`. That recipient then gets email (§8) until the row is cleared. Other Graph errors here are retried with backoff.

### 6.3 Send

`POST <serviceUrl>v3/conversations/<conversationId>/activities`:

```json
{"type": "message",
 "summary": "Recalling line_1042.segy",
 "attachments": [{"contentType": "application/vnd.microsoft.card.adaptive", "content": <card>}]}
```

The response's `id` is stored as `notification.teams_activity_id`, and the conversation id as `teams_conversation_id`.

### 6.4 Update in place

`PUT <serviceUrl>v3/conversations/<conversationId>/activities/<activityId>`, with the same body plus `"id": "<activityId>"`.

### 6.5 The `notify` consumer

- Each message `{"recall_id"[, "reason"]}` means "bring every card of this recall up to date". It is idempotent.
- Recipients: the requesters, plus owners and watchers per §3.1 at `requested`. Each gets a `notification` row (`recall_id`, `identity_id`, `channel`).
- **Coalescing:** a card is sent only if what it shows changed (`notification.last_card_key`: state, hold date, tracking, access, ETA), or the message has `"reason": "click"` (a click consumed an intent, so fresh ones are needed). It is sent at most once per 10 s per notification; later changes wait for `next_attempt_at`, and the message is re-queued with that delay.
- **Owners and watchers** get one card at `requested`; after that, updates only if they pressed Track.
- **States:** `queued` → `sent`; `failed` while retrying; `fell_back` (email from now on); `skipped` (no Entra match, no access, no mail: nothing more is sent).
- **Rate limits:** at most 7 sends per second per conversation and 50 per second per bot. VERIFY against Teams' published limits.
- **Retries:** 429 waits `Retry-After`. 5xx and timeouts back off from 10 s up to 15 min. After 1 h of failures, the notification falls back to email (if `notify.email_fallback`) and is marked `fell_back`.
- **403 or 404 from Bot Connector:** the user removed the app, or the conversation is gone. The worker clears `teams_conversation` and re-installs once. A second failure falls back to email.
- `notification.attempts`, `last_error` (a code, never a response body), `last_card_state`, `last_card_key`, `last_sent_at`, `failing_since` and `card_version` are kept current.
- **Sweep `notify_due`** (every minute): a notification whose `next_attempt_at` is more than 2 minutes past, with no `notify` message waiting, gets one re-queued.
- The consumer's HTTP calls run inside its transaction (so the rows and the ack commit together), each with a timeout of 10-15 s.

## 7. Recipient resolution

The identity chain (`AGENTS.md`): uid or SMB user → SID (edge, winbind) → Entra objectId → Teams.

- **SID → user:** `GET https://graph.microsoft.com/v1.0/users?$filter=onPremisesSecurityIdentifier eq '<SID>'&$select=id,displayName,userPrincipalName,mail,onPremisesSecurityIdentifier,onPremisesSamAccountName`.
  - Exactly one result fills `identity.entra_object_id`, `display_name`, `upn`, `mail`, `graph_resolved_at`, with `graph_state = resolved`.
  - Zero results: `not_found`. The identity is re-checked after 24 h (`graph_next_attempt_at`).
  - Errors: `error` (or `resolved` kept, if it was), with `graph_error` and a 5-minute backoff.
  - Another identity already holding that objectId: `error`, `object_id_conflict`.
- **Groups:** `GET …/users/<oid>/transitiveMemberOf/microsoft.graph.group?$select=id,onPremisesSecurityIdentifier&$top=999`, following `@odata.nextLink`.
  - `group_sids` = the non-null on-premises SIDs, plus `<user's domain SID>-513` (Domain Users) and `S-1-5-11` (Authenticated Users).
  - `groups_source = graph`, `groups_resolved_at = now`.
- **Freshness:** resolved before a card if the user's row is older than 24 h, and the groups if older than 1 h. Each identity is resolved by at most one worker at a time (`SELECT … FOR UPDATE SKIP LOCKED`).
- **`identity_resolve` queue:** `{"identity_id"}`, sent by `web` on token group overage and by `api` when a Teams click finds the clicker's groups older than 1 h; the consumer refreshes the user and groups at once, ignoring freshness. New SIDs from `api/recalls.py` need no message: the `notify` consumer resolves them before their first card.
- **No SID, or no Entra match:** no card and no email. Audited as `notify.skipped`, reason `no_identity` or `not_in_entra`.
- **Owners and watchers:** the group's objectId comes from its `identity` row (kind `group`, filled by the directory sync below); members are read with `GET …/groups/<oid>/transitiveMembers/microsoft.graph.user`, and each becomes an `identity` row.
- **Group directory** for the portal's group picker (and the line above): nightly (`group_directory` sweep), `GET …/groups?$filter=onPremisesSyncEnabled eq true&$select=id,displayName,onPremisesSecurityIdentifier,onPremisesSamAccountName`. It upserts `identity` rows of kind `group`.

## 8. Email fallback

- Used when Teams is blocked (§6.2), keeps failing (§6.5), or the policy turns Teams off for the share.
- `POST https://graph.microsoft.com/v1.0/users/<OMNIFILE_NOTIFY_SENDER>/sendMail`:

```json
{"message": {"subject": "Recalling line_1042.segy",
             "body": {"contentType": "HTML", "content": "…"},
             "toRecipients": [{"emailAddress": {"address": "<identity.mail>"}}]},
 "saveToSentItems": false}
```

- The body has the same facts as the card and a portal link. It has no buttons and no intents.
- One email at `requested`, one at `available`, `failed` or `superseded`. No mail for intermediate states, or for `cancelled`. If Teams falls back mid-recall, the first email describes the current state.
- `Mail.Send` must be limited to the sender mailbox by Exchange RBAC for Applications (VERIFY).
- A recipient without `mail` gets nothing. Audited as `notify.skipped`, reason `no_mail`.
- Rows: `notification` with `channel = 'email'`. The Teams row becomes `fell_back`.

## 9. Logging and audit

**Audit (`catalog.audit_log`),** actor `system:notify` for the worker, `user:<identity_id>` for clicks:

| Action | Decision | Details |
|---|---|---|
| `teams.card.sent` / `teams.card.updated` | info | recall id, notification id, state, audience, card version |
| `teams.card.failed` | info | recall id, notification id, error code |
| `email.sent` | info | recall id, notification id |
| `notify.skipped` | info | recall id, identity id, reason |
| `teams.intent.accepted` | allow | intent id, action, recall id or hold id |
| `teams.intent.refused` | deny | intent id (if parsed), action, reason code |
| `recall.request` (via teams) and `hold.set` | allow | as written by the existing paths and triggers |

**Logs** (JSON, like the relay's):
- event, outcome, reason, latency;
- intent id, recall id, notification id, identity id.

**Never logged:** tokens, intents, card or email bodies, display names, UPNs, mail addresses, objectIds, file names, paths. The audit log holds identities and file ids; the logs don't.

## 10. Configuration

| Variable | Where | Meaning |
|---|---|---|
| `OMNIFILE_TENANT_ID` | api, worker, web | The client's Entra tenant |
| `OMNIFILE_TEAMS_BOT_APP_ID` | worker | The bot's Microsoft App ID (same as the relay's `OMNIFILE_RELAY_BOT_APP_ID`) |
| `OMNIFILE_TEAMS_APP_ID` | worker | The Teams app's catalog id, for proactive install |
| `OMNIFILE_TEAMS_BOT_CREDENTIAL` | worker | `managed_identity`, or a path to a certificate or secret |
| `OMNIFILE_TEAMS_SERVICE_URL` | worker | Default `https://smba.trafficmanager.net/teams/` |
| `OMNIFILE_TEAMS_INTENT_SIGNING_KEY`, `OMNIFILE_TEAMS_INTENT_KID` | worker | §2 |
| `OMNIFILE_TEAMS_INTENT_PUBLIC_KEYS` | api | §2 |
| `OMNIFILE_TEAMS_RELAY_CERT_SHA256` | api | Comma-separated SHA-256 fingerprints (hex) of the relay certificates allowed to call (§4.1). Required unless `OMNIFILE_DEV_MODE=1` |
| `OMNIFILE_PORTAL_URL` | api, worker | For `Open in portal` links and email |
| `OMNIFILE_NOTIFY_SENDER` | worker | Mailbox UPN for `sendMail` |
| `OMNIFILE_GRAPH_CREDENTIAL` | worker | `managed_identity` (default; the worker's identity holds the Graph application permissions), or a path to a client secret |
| `OMNIFILE_GRAPH_CLIENT_ID` | worker | The client id for a secret credential (default: the bot's app id) |
| `AZURE_CLIENT_ID` | worker | The user-assigned managed identity, for `managed_identity` credentials |
| `OMNIFILE_GRAPH_URL`, `OMNIFILE_BOTCONNECTOR_TOKEN_URL`, `OMNIFILE_ENTRA_AUTHORITY` | worker | Default to Microsoft's. **Dev mode only:** point at the fakes; refused otherwise. In dev mode `OMNIFILE_TEAMS_SERVICE_URL` may be `http://` |

Without `OMNIFILE_TENANT_ID`, `OMNIFILE_TEAMS_BOT_APP_ID` and `OMNIFILE_TEAMS_APP_ID` the worker runs no notification jobs. Without a signing key it sends cards with no `Action.Execute` buttons. `api` reads its Teams settings on the first click and answers `503` without them.

## 11. Fakes (tests and dev)

In `service/src/omnifile_service/notify/fakes/`, Starlette apps:
- **Fake Graph:** users by `onPremisesSecurityIdentifier`, `transitiveMemberOf`, group `transitiveMembers`, synced groups, `installedApps` (with a switch that answers 403 for blocked users), `chat`, `sendMail` (records messages), and an Entra token endpoint at `/<tenant>/oauth2/v2.0/token` (client secret). Seeded from JSON (`FakeGraph.from_seed`); `page_size` forces `@odata.nextLink` paging; `fail` injects statuses per path.
- **Fake Bot Connector:**
  - a token endpoint;
  - `POST` and `PUT` activities, which record every card version per conversation;
  - a switch that answers 403, 404 or 429.
- **Fake Teams client** (`FakeTeams`): `click(conversation_id, button_title, clicker=<objectId>)` takes the latest card, builds the `invoke` activity (`name = adaptiveCard/action`, `value.action` with `type`, `verb` and `data`, `from.aadObjectId`, `conversation.tenantId`, `channelData.tenant.id`, `serviceUrl`, `timestamp`), and signs a Bot Framework JWT with a test RSA key whose JWKS it serves, including the `msteams` endorsement and the `serviceUrl` claim. It POSTs to the relay's `/api/messages` and returns the relay's reply.

The relay runs unchanged. Tests pass `create_app(settings, sink=…, http_client=…)` an `httpx` client whose transport (`FakeTeams.transport()`) serves the fake OpenID document and JWKS at Microsoft's own `login.botframework.com` URLs, so the relay's default settings apply. `service/tests/api/test_teams_intents.py` drives clicks this way: relay (from `relay/src`) → `api` (relay certificate in `X-Forwarded-Client-Cert`) → worker → fake Bot Connector.

## 12. Not yet confirmed on a real tenant

See `docs/milestones/real-tenant-checks.md`.
