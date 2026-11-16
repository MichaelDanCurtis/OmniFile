# Checks that need a real Microsoft 365 / Entra tenant

M4 is built and tested against the dev OIDC issuer, a fake Graph and a fake Bot Connector, with the relay's real code. Everything below can only be confirmed with a real Entra tenant that has Teams, synced from an on-prem AD. Run it in a dev tenant, or with the client in pilot 4. Each line names where the assumption lives. Storage and hosting checks stay in `real-azure-checks.md`.

Each line has an id (`RT-nn`). The real-cloud harness maps every id to an automated Graph check, a step of the M4 exit on the tenant, a step of the Azure run's M6 exit (`azure:<step>`), or a recorded manual step (`tests/real/harness/checks/tenant.py`; a test fails if a line has no mapping). `tests/real/run.sh all --cloud tenant --azure-run <id>` runs them against an Azure run that is up, in a tenant on `tests/real/allow-list.local`. The Teams steps ask the owner to confirm what only a person can see. `tests/real/collect.py` ticks a line only from a real run.

## M4 exit (from the build plan)

- [ ] RT-01 **A real Teams user in the dev tenant gets a card for their own stub read.** The AD user reads a stub over SMB or NFS Kerberos; the uid → SID → objectId chain resolves; the card arrives in their 1:1 chat with the bot. (`notify/`, `teams.md` §6–7)
- [ ] RT-02 **Clicking Recall reaches the relay and queues a recall:**
  - on a "failed" card: an injected failure in the dev subscription, or a cancelled restore;
  - the card then follows the new recall to `available`.

  (`relay/`, `api/teams.py`)
- [ ] RT-03 **An admin edits a policy and sees the cost update,** signed in through the real Entra app with app roles. (`web/`, `portal/`)

## Teams and Bot Framework

- [ ] RT-04 **Teams calls the relay only from the `AzureBotService` ranges** (decision 49). Deploy with `relayAllowedCidrs` taken by `deploy/azure/relay-ranges.sh --location <region>` from the global `AzureBotService` tag (required, no default: `deploy/azure/README.md`, "Closed by default").
  - **Clicks arrive from those ranges:** every card click and message still reaches the relay over a week; none is refused by the ingress (Container Apps logs a 403 for a refused source). Note the source addresses the relay sees.
  - **Across a Microsoft range update:** rerun the script (it prints the prefixes added and removed) and redeploy; nothing is dropped in between.
  - **The regional tag** (`--regional`): whether every call comes from the relay region's `AzureBotService.<Region>` prefixes alone. Only then narrow to it.
  - **The rule limit:** the number of prefixes the script reports against the Container Apps ingress IP rule limit (pass `--max-rules` once it is known).
  - **The fixture's shape:** a real `az network list-service-tags` output has the fields the script reads (`values[].name`, `properties.addressPrefixes`, `properties.region`, `changeNumber`), as `deploy/azure/testdata/service-tags.json` assumes from the REST reference. Fix the fixture's shape if not; keep its documentation addresses, since the repo records no copy of Microsoft's ranges.

  (`deploy/azure/relay-ranges.sh`, `main.bicep`, threat model T1)

- [ ] RT-05 **Proactive install** through Graph `POST /users/{id}/teamwork/installedApps` under the tenant's app setup and permission policies (risk R5).
  - What a blocked install returns (status and error code), so the email fallback triggers on it and nothing else.
  - Whether `TeamsAppInstallation.ReadWriteSelfForUser.All` is enough to read the installation's `chat`.

  (`notify/botconnector.py`, `teams.md` §6.2)
- [ ] RT-06 **The service URL for proactive messages:** `https://smba.trafficmanager.net/teams/` vs the regional form, for this tenant. (`OMNIFILE_TEAMS_SERVICE_URL`)
- [ ] RT-07 **Bot credential:** a user-assigned managed-identity bot works for Bot Connector tokens from Container Apps; otherwise use a certificate. Single-tenant token scope `https://api.botframework.com/.default`. (`teams.md` §6.1)
- [ ] RT-08 **Card send and update in place:**
  - `PUT …/activities/{id}` replaces a card sent days earlier;
  - updates within the published rate limits (7 per second per conversation, as assumed);
  - a 20 KB card renders in Teams desktop, web and mobile.

  (`teams.md` §3, §6.5)
- [ ] RT-09 **The invoke from a real click:**
  - `from.aadObjectId`, `conversation.tenantId`, `channelData.tenant.id` and `timestamp` present;
  - `value.action.verb` and `data` as sent;
  - the Bot Framework token's `serviceUrl` claim and `msteams` endorsement.

  Already listed for the relay in `real-azure-checks.md`; confirm once with the full path.
- [ ] RT-10 **The invoke response replaces the card** for the clicker (Universal Actions, `application/vnd.microsoft.card.adaptive`). The worker's update 6 s later lands after it, on desktop and on mobile. (`teams.md` §5)
- [ ] RT-11 **Action.Execute on Teams mobile** and in the new Teams client.
- [ ] RT-12 **A user who removes the app,** or blocks the bot: what Bot Connector returns, and that re-install once, then email, behaves. (`teams.md` §6.5)

## Graph

- [ ] RT-13 **`$filter=onPremisesSecurityIdentifier eq '<SID>'`** on `/users` works as a plain filter, without `ConsistencyLevel: eventual` and `$count`. Its latency at the client's directory size. (`notify/graph.py`)
- [ ] RT-14 **`transitiveMemberOf` returns `onPremisesSecurityIdentifier`** for synced groups, including nested ones, within the documented page size. (`teams.md` §7)
- [ ] RT-15 **Primary group:** Domain Users (`-513`) is added by rule, because Entra doesn't sync primary-group membership. Find users whose `primaryGroupID` isn't 513, and decide whether that matters for real ACLs. (`portal-api.md` §3.3)
- [ ] RT-16 **Group directory sync** (`onPremisesSyncEnabled eq true`) completes for the client's group count within the nightly window.
- [ ] RT-17 **`sendMail`** from the service mailbox with `Mail.Send` limited by Exchange RBAC for Applications (or an application access policy) to that one mailbox; a send as any other mailbox is refused. (`teams.md` §8)
- [ ] RT-18 **Admin consent** for the worker's managed identity: `User.Read.All`, `GroupMember.Read.All`, `TeamsAppInstallation.ReadWriteSelfForUser.All`, `Mail.Send`. Assigned as app roles to the managed identity's service principal. (`M4.md`, "For the owner")

## Entra sign-in (portal)

- [ ] RT-19 **The `groups` claim emitted as on-premises group SIDs** ("sAMAccountName / On Premises Group Security Identifier" in the token configuration) for synced groups, in the ID token. (`portal-api.md` §2.1, §3.3)
- [ ] RT-20 **The `onprem_sid` optional claim** is present for synced users.
- [ ] RT-21 **Overage:** above 200 groups the ID token carries `_claim_names` / `hasgroups`, and `web` falls back to the worker's Graph lookup. Test with a user in more than 200 groups.
- [ ] RT-22 **App roles assigned to AD-synced groups** appear in `roles` for members, including nested group members (Entra doesn't expand nested groups for app role assignment; VERIFY and document).
- [ ] RT-23 **Client assertion from `web`'s managed identity** through a federated identity credential on the app registration, at the token endpoint, with no secret. (`portal-api.md` §2.1)
- [ ] RT-24 **Re-authentication:** `prompt=login` with `max_age=0` returns a fresh `auth_time`, and WORM enable accepts it. (`portal-api.md` §2.4)
- [ ] RT-25 **Partner tokens:** a Prizmanyx client-credentials token has `idtyp=app`, the v2 issuer, our audience and the assigned app roles. (`portal-api.md` §2.2)
- [ ] RT-26 **Session cookie behaviour** on the private ingress: `__Host-` cookies with `Secure` behind Container Apps TLS; `SameSite=Strict` with the first navigation from a Teams card link.

## Private network

- [ ] RT-27 **Download links:** a browser on the client's LAN resolves and reaches the blob private endpoint named in a download SAS. If it can't, the portal's download needs another path (M5's `.snapshots`, or a proxy). (`portal-api.md` §5.4)
- [ ] RT-28 **`Open in portal` links** from Teams open on the client's network, and fail cleanly outside it.

## Teams track: assumptions made while building (M4)

- [ ] RT-29 **The chat id from Graph is the Bot Connector conversation id.** `GET /users/{id}/teamwork/installedApps/{installation}/chat` returns `19:…@unq.gbl.spaces`, and `POST {serviceUrl}v3/conversations/{that id}/activities` accepts it for the bot's 1:1 chat. (`notify/delivery.py` `_conversation`, `teams.md` §6.2)
- [ ] RT-30 **`installedApps?$expand=teamsApp&$filter=teamsApp/id eq '<app id>'`** works with `TeamsAppInstallation.ReadWriteSelfForUser.All` as an application permission, and the app id to filter on is the catalog id (`OMNIFILE_TEAMS_APP_ID`), not the manifest id. (`notify/graph.py`)
- [ ] RT-31 **Owner and watcher expansion:** `GET /groups/{id}/transitiveMembers/microsoft.graph.user?$select=…,onPremisesSecurityIdentifier` with `GroupMember.Read.All` returns nested members with their SIDs. (`teams.md` §3.1, §7)
- [ ] RT-32 **Card text escaping:** backslash-escaped Markdown (`\_`, `\.`, `\[`) in `TextBlock` and `FactSet` values renders as the plain character in Teams desktop, web and mobile, and a file name like `[x](https://…)` never becomes a link. (`notify/cards.py` `md_escape`)
- [ ] RT-33 **The activity `summary`** (for example "Recalling line_1042.segy") is what Teams shows in the notification banner and the chat list, for both the send and the in-place update. (`notify/botconnector.py`)
- [ ] RT-34 **Bot rate limit:** 50 sends per second per bot, as assumed alongside 7 per second per conversation; and the `Retry-After` header on a Bot Connector 429. (`notify/delivery.py` `Rate`, `teams.md` §6.5)
- [ ] RT-35 **Bot Connector status codes:** a user who uninstalled the app gives 403, a deleted conversation 404, and neither is returned for a transient fault, since both trigger a re-install. (`notify/botconnector.py` `BotError.gone`)
- [ ] RT-36 **The relay certificate through Container Apps ingress:** `api` sees the relay's certificate (SAN `urn:omnifile:relay`) in `X-Forwarded-Client-Cert` from the relay's environment, and refuses it on every edge path. (`api/clientauth.py` `RelayAuthenticator`)
- [ ] RT-37 **`auth_time` in the ID token:** with the `auth_time` optional claim on the app registration and the `claims` request parameter `/auth/login` sends, Entra puts `auth_time` in the ID token, and after `prompt=login&max_age=0` it is the time of that fresh sign-in, not of an earlier SSO session. Without it, WORM and legal-hold changes always answer `401 reauth_required`. (`web/auth.py` `AUTH_TIME_CLAIM`, portal-api.md §2.4)

## M4 integration: what the sim run stood in for

The M4 exit test (`tests/e2e/m4_exit_test.sh`, results in `docs/milestones/M4.md`) runs every process for real (`api`, `web`, worker, relay) but plays Microsoft and the edge. Each stand-in below needs its real counterpart once:

- [ ] RT-38 **The relay's Bot Framework keys from Microsoft.** In the test the relay's `http_client` fetches the OpenID metadata and JWKS from the fake Teams (`dev/m4/relay.py`); the relay code is unchanged. Confirm the external Container Apps environment reaches `login.botframework.com` and that keys rotate without a restart. (`relay/src/omnifile_relay/botauth.py`)
- [ ] RT-39 **Clock agreement between the relay and `api`.** `api` now checks `activity_time`, `received_at` and the intent's `iat`/`exp` against its wall clock (it used the service clock, which the dev virtual clock moves). Both Container Apps environments must keep NTP time within the 10-minute window. (`api/teams.py` `_precheck`)
- [ ] RT-40 **The edge's stub read, from a real AD user, end to end.** The sim run's "edge" is the test script with a dev-CA edge certificate calling `POST …/recalls` with the user's SID. Confirm with the real daemon: an SMB read (smb-guard) and an NFS Kerberos read (FSAL) each produce the user's SID, and Graph resolves that SID to the same user who signs in to the portal. (`api/recalls.py`, `notify/graph.py`)
- [ ] RT-41 **TLS in front of `web`.** The dev stack terminates TLS in uvicorn with a dev-CA certificate; in Azure the private ingress does. Confirm the `Origin` the browser sends equals `OMNIFILE_WEB_ORIGIN` behind the ingress (CSRF check), and that `/auth/callback` sees the public host name, not the container's. (`web/auth.py`, `web/context.py`)
- [ ] RT-42 **Browsers.** The browser tests run chromium only. Check the portal in Microsoft Edge and Safari on the client's managed desktops, and the "Open in portal" link from the Teams desktop and mobile clients (which may open an in-app browser without the portal session).
- [ ] RT-43 **Session sweep on two clocks.** The worker deletes expired portal sessions on the wall clock (`worker/portal_jobs.py` `sweep`); in Azure the worker and `web` share real time, so this only matters in dev. Nothing to check beyond NTP, listed for completeness.
- [ ] RT-44 **The card to a pin's setter (decision 66).** When another file's archive date moves a shared blob over a pin, because neither file could get its own copy, the setter gets one Teams card with no buttons, saying the pin wasn't kept, or one mail if the app is blocked. It's sent as other proactive cards are, with the fakes in `service/tests/notify/test_pin_notice.py`. Confirm the card renders on a real tenant, "Open in portal" works, and it names no other file.
- [ ] RT-45 **The card for a recall whose file was replaced or deleted** (control API §4.4, `teams.md` §3.2). The card a requester already has is updated in place to "*name* was replaced" or "*name* was deleted", with the one sentence and only "Open in portal"; a blocked app gets one mail instead. It is tested with the fakes in `service/tests/notify/test_delivery.py`. Confirm on a real tenant that the update renders, that the earlier card's Keep online button is gone, that "Deleted by *who*" shows the directory's display name for a delete made over SMB or Kerberos NFS by a real AD user, and that a delete over NFS `sec=sys` names no one.
