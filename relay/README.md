# OmniFile Teams relay

The only public endpoint in OmniFile Gateway (`AGENTS.md`, Networking and "Identity chain"). It receives Teams clicks on the Recall / Keep online / Track buttons, decides whether each click is genuine, and passes an accepted click inward to the private service. It holds no data access, no bot credentials and no signing key.

```
Teams ──HTTPS──► relay (public)  POST /api/messages
                   │ 1. Bot Framework JWT (Microsoft's signature, our app id, serviceUrl, channel)
                   │ 2. tenant is ours (single-tenant bot)
                   │ 3. signed intent (Ed25519, ours) — unexpired, unused, untampered
                   │ 4. clicker == intent's recipient
                   ▼
           private `api` (VNet, mTLS)  POST /internal/v1/teams/intents
                   │ re-verifies the intent, dedupes by intent_id, checks ACL, enqueues (PGMQ)
                   ▼
           worker updates the card later (service holds the bot credentials)
```

Python 3.12, Starlette + uvicorn, PyJWT + `cryptography`, httpx. Apache-2.0 like the rest of the repo.

```
uv sync
uv run ruff check . && uv run ruff format --check . && uv run pytest -q
```

## What it checks

### 1. The Bot Framework token

Per Microsoft Learn, *Authenticate requests with the Bot Connector API*, section "Authenticate requests from the Bot Connector service to your bot" (read 2026-10-05). Implemented in `src/omnifile_relay/botauth.py`:

| Microsoft requirement | Here |
|---|---|
| `Authorization: Bearer <jwt>` | Anything else is 401. |
| OpenID metadata at `https://login.botframework.com/v1/.well-known/openidconfiguration` (static, may be hard-coded) | Hard-coded. `jwks_uri` must be https on `login.botframework.com`, or the document is ignored. |
| Keys "may be cached"; refresh "at least once every 24 hours" | Refreshed every 6 h. An unknown `kid` forces an early refresh, at most once per 5 min, so rotation works without a restart and random kids can't make us hammer Microsoft. If Microsoft is unreachable, the last keys are used for up to 48 h, then requests fail closed with 503. |
| `iss` = `https://api.botframework.com` | Exact. The Emulator / MSA issuers are not accepted (production only). |
| `aud` = our Microsoft App ID | Exact. |
| Validity period, 5 min clock skew | `exp` required; `nbf` checked; 300 s leeway. |
| Signature with a key from the JWKS, using an algorithm in `id_token_signing_alg_values_supported` | Header `alg` must be RS256/RS384/RS512 **and** listed in the metadata. `none`, HS*, and key confusion are rejected. |
| `serviceUrl` claim equals the activity's `serviceUrl` | Exact string match. |
| Endorsements: the signing key's `endorsements` must include the activity's `channelId`, else 403 | Enforced; `channelId` must also be `msteams`. |
| No way to disable validation | There is no such setting (a test asserts it). |

**Single tenant.** Bot Connector tokens identify the bot, not the tenant, so the tenant is checked on the activity: `conversation.tenantId` and `channelData.tenant.id` must be present and equal our tenant, and a `tid` claim, if present, must match too. The intent's `tid` must match as well.

### 2. The signed intent

The service mints one token per button when it sends the card and puts it in `Action.Execute` `data` as `omnifile_intent`. It is a JWS signed with Ed25519:

```
header  {"alg":"EdDSA","typ":"omnifile-intent+jwt","kid":"<key id>"}
claims  jti  intent id (random, single use)       act  recall | keep_online | track
        fid  catalog file id                       sub  recipient's Entra objectId
        tid  tenant id                             iat, exp
        iss  "omnifile-service"                    aud  "omnifile-teams-relay"
```

About 560 bytes, well inside a card (the relay refuses anything over 2 KiB). The relay holds only public keys (`OMNIFILE_RELAY_INTENT_PUBLIC_KEYS`, several `kid`s allowed for rotation), so it can check intents but never make one. Rejected: bad signature, unknown `kid`, wrong `typ`/`aud`/`iss`, expired (60 s leeway), `iat` in the future, lifetime over 14 days, unknown action, malformed ids. The button's `verb` must equal the signed `act`.

The service should sign intents with a dedicated key (not one used for anything else); `typ` and `aud` stop a token made for another purpose being accepted here.

### 3. The clicker

`from.aadObjectId`, from an activity whose token we have verified, must equal the intent's `sub`. A forwarded or shared card does nothing for anyone else.

### 4. Single use

Before forwarding, the relay reserves the intent id until the intent expires. A second click, or one of Teams' two automatic retries, gets "We already have this request" and is not forwarded again. If forwarding fails, the reservation is released so the user can retry. The store is per replica, so **the service is the authority**: it must treat `intent_id` as an idempotency key (a unique constraint), and answer 409 for a repeat.

## The reply and Teams' time limit

Teams waits **5 seconds** for an invoke response. If none comes, it retries twice, then shows "Unable to reach the app", and it ignores a late reply (Microsoft Learn, Teams platform docs). The relay gives the service 3 seconds (`forward_timeout_seconds`, which the config refuses to set at 4.5 s or more). The service only enqueues, so this is ample.

The response follows the Universal Action Model: HTTP 200 with `{"statusCode", "type", "value"}`:

- accepted: `application/vnd.microsoft.card.adaptive` with the card the service returned, or a plain "Recall requested…" card without buttons;
- duplicate: `application/vnd.microsoft.activity.message`;
- refused: `application/vnd.microsoft.error` with `statusCode` 400 / 403 / 500 and a generic message (no file names, no identities).

The relay can't mint intents, so cards it returns have no buttons. Later states (requested → rehydrating → downloading → available) are sent by the service, which holds the bot's credentials. Other activities (messages, installs, conversation updates) get an empty 200 and are not forwarded; other invoke names get 501.

## Forwarding inward

The relay must be able to say "this verified person clicked this signed intent" and nothing else. Options:

| Option | Relay can… | Verdict |
|---|---|---|
| **A. HTTPS + mTLS to the private `api`** over the VNet, one narrow endpoint `POST /internal/v1/teams/intents` | call one endpoint, as a client certificate the `api` maps to a "relay" role that can do only this | **Recommended** |
| B. Write to PGMQ in the catalog Postgres | log in to the catalog database | Rejected: gives the internet-facing box a database credential, which is data access. |
| C. Azure Service Bus or Storage queue | send to one queue (with a send-only identity) | Workable, but adds a broker and a private endpoint, and decision 27 already chose PGMQ over Service Bus. The service would need a consumer anyway. |
| D. The relay calls Bot Connector itself to update cards | — | Rejected: needs the bot secret on the public box. |

With **A**:

- The body is the service's `IntentForward` schema (`service/openapi/control-api.yaml`; `tests/test_contract.py` checks it): `intent` (the **raw signed token**), `intent_id`, `action`, `clicker_aad_object_id`, `tenant_id`, `activity_time` (the activity's `timestamp`, required; a click without a valid one is refused) and `received_at`, all times RFC 3339 UTC. It does not send the `serviceUrl`, conversation ids or any free text, which an attacker could steer. Sending the raw token means the service re-verifies the Ed25519 signature itself, so a compromised relay cannot invent an intent; at most it can replay intents it has seen, which the service dedupes.
- It sets `Idempotency-Key: <intent_id>`. The `api` answers 2xx (optional `{"card": …}` or `{"message": …}`), 409 (duplicate), 400/403/404/410/422 (refused, for example the user can no longer read the file per `docs/design/access-control.md` §5), or 5xx (transient; the relay releases the reservation).
- TLS ≥ 1.2, a private CA bundle, no redirects, and proxy environment variables ignored. `api` requires a client certificate from the OmniFile CA: by default in its own TLS handshake, behind a TCP ingress (`apiIngress = tcp`, threat model T5), or at an HTTP ingress (`clientCertificateMode: require`) that forwards it in `X-Forwarded-Client-Cert`. The relay gets its own certificate from the OmniFile CA (`python -m omnifile_service.enrol issue-relay-cert`), distinct from edge certificates, and `api` accepts it only while its SHA-256 is pinned (`relayCertSha256`).

The interface is `IntentSink` in `src/omnifile_relay/sink.py`; `HttpsMtlsSink` is the implementation, and tests use a fake.

**Where it runs.** `ARCHITECTURE.md` §1 leaves this open (VERIFY). Suggested: a separate small **external** Container Apps environment (or a Container App with external ingress in a separate subnet), with VNet integration into a subnet that can reach the internal environment's static IP through private DNS, and egress limited (NSG or Azure Firewall) to `login.botframework.com:443` and the internal `api`. The relay makes **no other outbound calls**: in particular, it never calls `serviceUrl`.

## Operations

### What a deployment must set

The process listens on **port 8080** (HTTP; TLS ends at the Container Apps ingress). Point the ingress `targetPort` and the Azure Bot's messaging endpoint (`https://<relay host>/api/messages`) at it. If a variable marked "yes" is missing or invalid (before the deployment's second phase, the certificate and the intent keys), it starts **not ready**: one log line names what is missing, and every path, `/healthz` included, answers 503, so Container Apps routes it no traffic and doesn't restart it (its liveness probe is the port); `OMNIFILE_RELAY_FORWARDED_ALLOW_IPS` has a default, so the deployment must remember to set it.

| Variable | Required | Meaning |
|---|---|---|
| `OMNIFILE_RELAY_BOT_APP_ID` | yes | The bot's Microsoft App ID (the JWT audience). |
| `OMNIFILE_RELAY_TENANT_ID` | yes | The tenant of the Teams users who click the cards (Bicep `teamsTenantId`; the deployment's own tenant when empty). |
| `OMNIFILE_RELAY_INTENT_PUBLIC_KEYS` | yes | JSON `{kid: key}`; each key is a PEM SubjectPublicKeyInfo or base64url raw 32 bytes (Ed25519 only). Public keys only. |
| `OMNIFILE_RELAY_SERVICE_BASE_URL` | yes | `https://…` of the private `api`. |
| `OMNIFILE_RELAY_SERVICE_CLIENT_CERT` | yes | Path to the relay's client certificate (PEM), mounted from Key Vault. |
| `OMNIFILE_RELAY_SERVICE_CLIENT_KEY` | yes | Path to its private key (PEM). |
| `OMNIFILE_RELAY_SERVICE_CA_BUNDLE` | yes | Path to the CA that issued the server certificate `api` presents. With TCP ingress (the default, `apiIngress = tcp`) that is the **OmniFile edge CA** (`edgeCaCertPem`, `deploy/azure/README.md` step 7): `api` terminates TLS itself with a certificate `enrol` signs from it. (Behind an HTTP ingress, which the template no longer offers for `api`, it would be the platform's issuing chain for the environment's default domain, read with `openssl s_client -connect <api fqdn>:443 -showcerts` from the relay's network.) Never a broad public-CA bundle (M7 review). |
| `OMNIFILE_RELAY_FORWARDED_ALLOW_IPS` | **yes in production** | The ingress proxy's address range (CIDR). Passed to uvicorn's `forwarded_allow_ips`, so `X-Forwarded-For` is believed only from the ingress. Default `127.0.0.1`: behind Container Apps, the per-IP rate limit would then see only the proxy's address and limit everyone together. Never `*`, which lets any client spoof its address and dodge the limit. |
| `OMNIFILE_RELAY_SERVICE_URL_HOSTS` | no | Comma-separated serviceUrl hosts. Default `smba.trafficmanager.net` (public cloud). Add the GCC / GCC High / DoD hosts only for those clouds. |
| `OMNIFILE_RELAY_MAX_CONCURRENCY` | no | uvicorn connection cap (default 200). |
| `PORT` | no | Listening port (default 8080). |
| `OMNIFILE_RELAY_LOG_LEVEL` | no | Default `INFO`. |

### Behaviour

- `GET /healthz` is liveness only. It never depends on Microsoft or the service being reachable; it reports whether signing keys are loaded.
- Container: `Containerfile` (multi-stage, uv-locked, `python:3.12-slim` and the uv image pinned by digest, UID 10001, no shell tools added, works with a read-only root filesystem; about 250 MB unpacked).
- Limits: 64 KiB bodies, read within 2 s (a slow sender gets 408); malformed or deeply nested JSON gets 400; 20 req/s per IP (burst 40) before any crypto; 1 click/s per user (burst 5) after authentication.
- Logs are JSON, one line per request: `event`, `outcome`, `reason`, `status`, `latency_ms`, `request_id`, and for intents `intent_id` and `action`. The formatter emits only an allowlist of fields. **Never logged:** tokens, objectIds, tenant ids, names, file ids, client IPs, `serviceUrl`; uvicorn's access log is disabled because it prints IPs.

## Threat model

The relay is on the internet. Assets behind it: recall budget (Azure's 10 GiB/h priority cap, S3 restore costs), users' trust in Teams cards, and the private service.

**An attacker on the internet can:**
- send any HTTP request to `/api/messages`. It is rate limited per IP before any crypto, capped at 64 KiB and a 2-second body deadline (no slow-loris), parsed safely (deep nesting is a 400, not a crash), and refused unless it carries a Bot Framework token signed by Microsoft for **our** app id.
- see a card someone forwards or screenshots. The intent in it only works when clicked by its recipient, inside Teams, once, before it expires.
- flood with garbage. The cost is a JSON parse and a signature check at worst. Unknown `kid`s can't force more than one key fetch per 5 minutes.
- take the relay down (DoS). That only stops Teams buttons; the portal and `POST /v1/recalls` still work, and shares are unaffected.

**An attacker cannot:**
- **forge a Bot Framework JWT.** It needs Microsoft's private key. `alg=none`, HS256 key confusion, other algorithms, other issuers (including the Emulator's), other audiences, expired tokens, and keys outside Microsoft's JWKS are rejected. A real token for **another** bot fails the audience check.
- **reuse a real token from another bot or tenant.** The audience is our app id; the activity's tenant and any `tid` claim must be ours.
- **forge or alter an intent.** Ed25519 signature; the private key is only in the service. Changing the file, action, recipient or expiry breaks the signature. The relay has only the public key, so even full compromise of the relay can't mint intents.
- **click someone else's card.** The clicker's objectId comes from an activity signed by Microsoft and must equal the signed recipient.
- **replay a click.** The intent id is single use at the relay and, authoritatively, at the service. Expired intents are refused.
- **use `serviceUrl` for SSRF.** The relay never makes a request to `serviceUrl`. It is checked against an allowlist (https, exact host `smba.trafficmanager.net`, port 443, no credentials, no whitespace or backslashes) before anything else, must equal the token's `serviceUrl` claim, and is not forwarded inward. The service sends card updates using the conversation reference it stored when it sent the card, never one taken from a click.
- **reach the service through the relay with arbitrary input.** The relay calls one fixed URL from configuration, with a fixed body schema; redirects are not followed and proxy settings are ignored.
- **learn about files or people from errors or logs.** Error messages are generic, and logs carry no personal data.

**Residual risks and their owners:**
- Microsoft's signing key or a Teams account is compromised: out of our control. A compromised account can click only its own cards, and the service still checks the file ACL.
- Relay host compromise: the attacker gains the relay's mTLS client certificate and the cards' intents seen in traffic. Mitigations: the service re-verifies every intent and dedupes; the certificate's role allows only this endpoint; rotate the certificate. The relay has no database, storage, Graph or bot credentials to steal.
- Per-replica replay store: with several replicas, a duplicate click could reach the service twice. The service's idempotency key makes that harmless.
- Clock skew on the relay host: 5 minutes is tolerated for Microsoft tokens, 60 s for intents. Run NTP (Container Apps does).
- To verify at M4 with a real tenant (BUILD-PLAN M4 exit): that real Bot Connector tokens for a single-tenant bot carry the `serviceUrl` claim and `msteams` endorsement as documented, and the exact `serviceUrl` hosts the client's Teams uses.
