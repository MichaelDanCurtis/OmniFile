# Azure test (pilot 4) — storage part

This kit covers the **storage** half of `docs/PILOTS.md` #4: Smart tier
ingestion, the smart → archive copy, and timed recalls. It does **not**
cover Storage Mover (that's a separate, larger spike per decision 20/24 in
`docs/DECISIONS.md`) or the Teams/identity checks, which are manual and
described at the end of this file.

Everything here is Bash + Azure CLI (`az`), idempotent, and Entra-auth-only
(`--auth-mode login` everywhere; shared key access is disabled on both
accounts at creation time). No secrets are written to disk. Nothing here
touches AWS, Storage Mover, or the edge filer.

## What's in here

| Script | Does |
|---|---|
| `lib/common.sh` | Shared env-var validation, logging, timing helpers. Sourced by every other script, never run directly. |
| `01-create.sh` | Creates the live (ZRS, Smart default tier) and archive (LRS) storage accounts + one container each. Prints (does not run) the private-endpoint commands for decision 22. |
| `02-smart-to-archive.sh` | Uploads a test file with no explicit tier, confirms Smart management, server-side copies it into the archive account at tier Archive, verifies, deletes the live original. Times every step; records whether the cross-account copy needed a SAS. |
| `03-recall.sh` | Rehydrates an existing Archive-tier blob by copy (never Set Blob Tier) at Standard or High priority, polling every 10 minutes, logging every poll to a CSV. Never estimates or promises a completion time. |
| `99-cleanup.sh` | Deletes the two storage accounts (and optionally the whole resource group). Warns about the Archive early-deletion fee first. |

All scripts start with `# VERIFY:` comments anywhere we could not fully
confirm current Azure CLI/REST behavior against Microsoft's docs on
2026-09-26 — search for `VERIFY` before you run something you're unsure
of, and re-check against `learn.microsoft.com` if the note says to.

## Prerequisites

- **Azure CLI** `az`, version **2.87.0 or later** — this is the minimum
  version Microsoft documents for the native `--access-tier Smart` flag
  ([client tooling table](https://learn.microsoft.com/azure/storage/blobs/access-tiers-smart#client-tooling)).
  Check with `az version`. If you're on an older version, `01-create.sh`
  detects this automatically and falls back to `az rest` against ARM
  (see the `# VERIFY:` comment in `01-create.sh` about the exact
  api-version string used in that fallback, in case it's since been
  superseded) — but upgrading (`az upgrade`) is simpler and recommended.
- Signed in: `az login` (interactive or `az login --identity` if you're
  running this from a VM/Cloud Shell with a managed identity that has the
  right roles).
- **RBAC roles**, assigned to whichever principal runs these scripts, on
  the resource group (or subscription) you'll create the accounts in:
  - **Storage Account Contributor** — to create/update/delete the storage
    accounts themselves.
  - **Storage Blob Data Contributor** — to upload, copy, tier, and delete
    blobs via `--auth-mode login`.
  - **Storage Blob Data Owner** — only needed if `02-smart-to-archive.sh`
    or `03-recall.sh` falls back to generating a user-delegation SAS (see
    "SAS vs. Entra" below); that fallback calls `az storage blob
    generate-sas --as-user`, which needs this role (or equivalent rights
    to read the account's user delegation key) on the **source** account.
    Grant it up front so a mid-run RBAC gap doesn't stall a multi-hour
    recall test.
  - **Contributor** (or narrower: Resource Group Contributor) on the
    resource group, if `01-create.sh` needs to create it.
- A region that supports **Smart tier + ZRS** (Smart tier is GA in public
  cloud regions with zonal redundancy as of this writing; it's in preview
  for Azure Government / 21Vianet — see the docs link above if you're
  targeting either of those clouds).
- Enough quota in the target subscription/region for two more general
  purpose v2 storage accounts.

You do **not** need `jq` or `python3` — everything here uses `az`'s own
`--query`/`-o tsv` for parsing, to keep the dependency list to Bash + `az`
as specified.

## Configuration (environment variables)

All scripts read these; none are hardcoded, and none default to a real
subscription/resource group (so a typo can't silently point at the wrong
tenant).

| Variable | Required | Meaning |
|---|---|---|
| `AZ_SUBSCRIPTION` | yes | Subscription id or name. Passed straight to `az account set --subscription`. |
| `AZ_RESOURCE_GROUP` | yes | Resource group name. Created by `01-create.sh` if it doesn't exist. |
| `AZ_REGION` | yes | Azure region, e.g. `eastus2`. Must support Smart tier + ZRS. |
| `AZ_NAME_PREFIX` | yes | Lowercase letters/digits only, 1-18 characters. Storage account names are `${AZ_NAME_PREFIX}live` and `${AZ_NAME_PREFIX}arch` — both must be **globally unique** across all of Azure, so pick something specific to this pilot (e.g. `omnipilot7x2q`, not `test`). |
| `AZ_CONTAINER_NAME` | no (default `segy-pilot`) | Blob container name in both accounts. |
| `AZ_TEST_STATE_DIR` | no (default `./.state` next to the scripts) | Where generated test files and CSV logs are written. |

Example:

```bash
export AZ_SUBSCRIPTION="00000000-0000-0000-0000-000000000000"
export AZ_RESOURCE_GROUP="rg-omnifile-pilot4"
export AZ_REGION="eastus2"
export AZ_NAME_PREFIX="omnipilot9k3f"   # must be globally unique
```

## Running it

```bash
./01-create.sh

# Uses a generated 1 MiB random file by default. For a representative
# result, point it at one of the client's real SEG-Y files instead:
TEST_FILE_PATH=/path/to/sample.segy ./02-smart-to-archive.sh

# docs/PILOTS.md asks for 8 GB, 184 GB and 1.2 TB recalls. This script
# does NOT generate those files (see 03-recall.sh's header comment for
# why) -- upload and archive the client's own files at those sizes first
# (e.g. by re-running 02-smart-to-archive.sh against each one, or by any
# other path that lands them in the archive account at tier Archive),
# then time the recall of each:
./03-recall.sh my-8gb-file.segy Standard
./03-recall.sh my-8gb-file.segy High my-8gb-file.segy.recalled-high
./03-recall.sh my-184gb-file.segy Standard
./03-recall.sh my-184gb-file.segy High
./03-recall.sh my-1.2tb-file.segy Standard
./03-recall.sh my-1.2tb-file.segy High

# When you're done:
./99-cleanup.sh                          # deletes both storage accounts
AZ_DELETE_RESOURCE_GROUP=true ./99-cleanup.sh   # also deletes the resource group
```

`03-recall.sh` polls every 10 minutes (`POLL_SECONDS`, default 600) and
never exits with an estimate or promise — it only prints and logs what
actually happened. For the 184 GB and 1.2 TB files at Standard priority in
particular, expect this to run for a long time; run it under `nohup`,
`tmux`, or similar if you need to disconnect, and re-run the same command
later to resume polling (the copy itself keeps running server-side
regardless of whether this script is watching it).

## SAS vs. Entra for the cross-account copy

`docs/PILOTS.md` explicitly asks us to check this, and it matters for the
product: OmniFile's edge holds no Azure secrets and mints short-lived SAS
tokens from the service (see `docs/AGENTS.md`'s "no Azure secrets on the
edge" design), so knowing whether a plain Entra/RBAC identity is enough
for a cross-account server-side copy — or whether a SAS is unavoidable —
directly affects that design.

What we found in Microsoft's own docs and community reports before ever
running this (recorded here so you can compare it against what actually
happens on the client's tenant): `az storage blob copy start`'s
`--source-uri` parameter is documented as needing the source to be
"public or authenticated via a shared access signature" for a
cross-account source, and this is reported to hold for the
`--source-account-name`/`--source-container`/`--source-blob` form too —
neither sets the newer `x-ms-copy-source-authorization` bearer header that
would let a pure Entra/RBAC identity work; **AzCopy** is the tool
documented to set that header for Entra-to-Entra cross-account copies.
`# VERIFY:` this was not confirmed against a live cross-tenant test by us
— only against docs and secondary sources — so `02-smart-to-archive.sh`
and `03-recall.sh` both **try Entra-only first anyway** and only fall back
to a short-lived user-delegation SAS (scoped to that one blob, read-only,
default 30-60 minute expiry, generated with `--as-user` so no account key
is ever touched) if the Entra-only attempt fails. Both scripts log which
path they ended up using — check `copy_auth_path` in
`02-smart-to-archive-timings.csv`, and the log line in `03-recall.sh`'s
output — and send us that result either way; a positive "Entra alone
worked" is just as useful to us as the expected "needed a SAS."

## Confirming Smart tier, honestly

`01-create.sh` sets the live account's default access tier to `Smart` and
`02-smart-to-archive.sh` checks `az storage account show --query
accessTier` to confirm that stuck, and checks the uploaded blob's
`properties.blobTier` after upload. `# VERIFY:` we could not confirm
whether an individual blob's `blobTier` property reports something
Smart-specific, or just the underlying capacity tier (Hot/Cool/Cold) with
no separate "is this blob Smart-managed" flag — Microsoft's docs describe
the `SmartHot`/`SmartCool`/`SmartCold`/`SmartHot-small` categorization as a
**Monitoring → Metrics** concept (Storage account → Monitoring → Metrics,
metric namespace **Blob**, metric **Blob Count** or **Blob Capacity**,
split by dimension **Blob tier**), not a per-blob CLI/REST property. After
running `02-smart-to-archive.sh`, also check that Metrics view in the
Portal for the live account and note what you see (even "too soon to show
up" is useful information — the doc doesn't say how quickly the Metrics
view reflects a just-uploaded blob).

## What to send back

1. `az version` output, and which code path `01-create.sh` took (native
   `--access-tier Smart` vs. the `az rest` fallback).
2. `~/.state/02-smart-to-archive-timings.csv` (or wherever
   `AZ_TEST_STATE_DIR` points) — one row per run. In particular:
   `tier_after_upload` (does it say `Hot` or something else? see the
   Smart-tier caveat above), `copy_auth_path` (`entra-only` or
   `user-delegation-sas`), and the per-step timings.
3. `~/.state/03-recall-timings.csv` — the full poll history for every
   file size and priority you ran. We care about the shape of the curve
   (does High actually finish faster than Standard once you're above the
   10 GiB/hour cap? does the cap bind at all for the smaller file?), not
   just the final number.
4. What the Portal's Metrics blade showed for Blob tier on the live
   account after the Smart-tier test (see previous section).
5. Whichever `# VERIFY:` comments you hit and what you found — especially
   the SAS-vs-Entra outcome and the exact `az rest` api-version behavior
   if you were on az CLI < 2.87.0.
6. Region and subscription type used (this affects Smart tier
   availability and, for a sovereign/government cloud, whether Smart tier
   is even GA yet — see the client-tooling note above).

## Manual checks (Teams + identity) — not scripted

`docs/PILOTS.md` also asks pilot 4 to prove out the Teams notification and
AD→Entra identity chain end to end. These are one-off, mostly-portal steps
that don't benefit from scripting, and touch a different Azure resource
type (Bot Service) and Microsoft Graph rather than Storage — do them once,
by hand, using a test user in the client's own tenant:

1. **Register a bot.** Create an **Azure Bot** resource
   (`Microsoft.BotService/botServices`), **single-tenant** (not
   multi-tenant), backed by an app registration with a client secret or
   certificate. `# VERIFY:` exact current portal flow / `az bot create`
   syntax — this changes occasionally; check
   [learn.microsoft.com/azure/bot-service](https://learn.microsoft.com/en-us/azure/bot-service/)
   at test time rather than trusting any specific command here to still
   be current.
2. **Install the Teams app for a test user, proactively, via Graph** (this
   is what lets the bot message a user who has never installed or
   messaged it first — required for the "Day 45 recall" notification
   flow):
   ```bash
   az rest --method post \
     --url "https://graph.microsoft.com/v1.0/users/<test-user-object-id>/teamwork/installedApps" \
     --headers "Content-Type=application/json" \
     --body '{"teamsApp@odata.bind": "https://graph.microsoft.com/v1.0/appCatalogs/teamsApps/<teams-app-id>"}'
   ```
   Requires the `TeamsAppInstallation.ReadWriteForUser.All` (or
   `.ReadWriteSelfForUser` if scoped narrower) Graph application
   permission, admin-consented in the client's tenant.
3. **Send a proactive Adaptive Card with `Action.Execute` buttons** to
   that user via the Bot Framework's conversation API (not Graph) — this
   needs the bot's own proactive-messaging flow (get or create a
   conversation reference for the installed app, then POST an Activity
   containing the Adaptive Card attachment). This part is SDK/code, not a
   single `az` or `curl` call — see the Bot Framework's proactive
   messaging samples for your chosen bot SDK/language.
4. **Resolve an AD SID to an Entra user object ID**, the identity-chain
   step from `docs/AGENTS.md`:
   ```bash
   az rest --method get \
     --url "https://graph.microsoft.com/v1.0/users?\$filter=onPremisesSecurityIdentifier eq '<AD-SID-in-S-1-5-...-form>'"
   ```
   Requires the `User.Read.All` Graph application permission (or
   `User.Read.All`/`Directory.Read.All` delegated, depending on how you
   authenticate the caller), admin-consented in the client's tenant. Use
   a real AD-synced test user's SID (`wmic useraccount get sid` on the
   domain, or `Get-ADUser <user> -Properties SID` from PowerShell on a
   domain-joined machine) — don't fabricate one, since the point of this
   check is confirming the sync + Graph filter actually resolves a real
   identity.

Send us: whether the proactive install succeeded (and any tenant policy
that blocked it, if it didn't), a screenshot or transcript of the
delivered Adaptive Card with working buttons, and the Graph response for
the SID lookup (object id is fine to share; the SID itself, treat as you
would any other AD identifier per the client's data-handling rules).
