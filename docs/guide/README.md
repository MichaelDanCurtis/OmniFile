# OmniFile Gateway: customer guide

This guide takes an Azure administrator from nothing to a working share: the OmniFile service
deployed in your Azure subscription, one edge filer running in your data centre, and one share
that people reach over SMB and NFS, with files that go to Azure, tier down to Archive and come
back when someone opens them.

Follow it from top to bottom. Each step says what it does, what you type or run, and what you
should see when it worked. If a step doesn't give that result, stop and look in
[Troubleshooting](#16-troubleshooting) before going on.

Plan for about a day of work spread over two or three days: the deployment itself takes about an
hour, but a few steps wait on other people (network, DNS, Teams) or on Azure (an Archive recall
takes hours).

## How to read this guide

**Where each step runs.** Every command or answer is in a box marked with a step number and the
place it runs:

| Marked | Where it runs |
|---|---|
| `on=workstation` | Your admin workstation, in a terminal, in the release folder (section 1.3). |
| `on=ad-admin` | A Windows machine with the Active Directory PowerShell module, as a domain admin. |
| `on=exchange-admin` | PowerShell with the `ExchangeOnlineManagement` module, as an Exchange administrator. |
| `on=dns-server` | One of your on-premises DNS servers (Windows DNS), in PowerShell as an admin. |
| `on=browser` | A web browser, signed in to a Microsoft admin centre. |
| `on=portal` | The OmniFile portal, in a web browser. Each line is one action: `open` a page, `click` a button or option, `fill` a field, `choose` from a list, `expect` some text on the page. |
| `on=hyperv`, `on=vsphere`, `on=kvm` | Your hypervisor, for the edge filer's VM (pick the one you use). |
| `on=edge-console` | The edge filer's console (the VM's screen). Each line is one answer to a question, in order. `<enter>` means press Enter to keep the answer shown in brackets. |
| `on=edge-shell` | A shell on the edge filer, signed in over ssh as an AD administrator of the filer. |
| `on=edge-network` | A Linux machine on the edges' network (inside `OMNI_EDGE_CIDRS`), with OpenSSL 3 or later and a copy of the release folder: the hypervisor host will do. `api` admits only that network and the relay. |
| `on=linux-client` | A Linux machine on your users' network (inside `OMNI_USER_CIDRS` and `OMNI_NFS_CLIENTS`) that mounts the share. |

**Your values.** Step 4 creates `omnifile.env`, where you write down your names and addresses once.
Every workstation step starts with `. ./omnifile.env && . ./omnifile.state.env`, which loads them,
so you can run any step in a new terminal. Steps that learn something (an output of the
deployment, a certificate) record it in `omnifile.state.env` themselves.

Steps that run anywhere else can't read that file. The steps on the filer (`on=edge-shell`) begin with
`set -euo pipefail`, so they stop at their first error: type `bash` on the filer first and paste the
step into that shell, so a failure ends it rather than your ssh session. In them, `<value:NAME>` stands for the value of
NAME from `omnifile.env`: in a console answer or a portal step, type it; in a command, replace
`<value:NAME>` with it before you run the command. `<secret:NAME>` means "type this password; it is
never written down".

**Steps that need a person** are marked `manual=yes`: a browser sign-in, or text you copy from
one window to another. `input=NAME` means the command asks for that value; type or paste it.

**Two marks are for test runs only** and change nothing for you: `plan=yes` on the read-only steps
the first deployment preview needs, and `gate=whatif` on each preview (steps 14, 41 and 76), whose
digest a test run's owner approves before that deployment changes anything.

**Steps that wait** are marked `wait=N`: they check something that takes time (a file becoming a
stub, a recall), and say so when it isn't ready yet. Run them again until they succeed, for up to N
minutes.

**Errors stop a step.** Run each workstation step with `docs/guide/tools/run-step.sh N` (N is the
step's number), from the release folder: it runs the step exactly as written here, and stops at the
first error. You can also paste the commands into a terminal yourself; then check each one worked before
the next. Run a failed step again once you've fixed the cause: every step is safe to repeat.
`run-step.sh --show N` prints a step that runs somewhere else.

## 1. Before you start

### 1.1 People and rights

You need these people, or one person with all of these rights:

| Who | For |
|---|---|
| **Azure:** Owner of the subscription, or Contributor plus User Access Administrator | The deployment creates role assignments for the service's identities. |
| **Entra:** Application Administrator and Privileged Role Administrator (or Global Administrator) | The portal's app registration, and giving the service's identity its Microsoft Graph permissions (that is the admin consent). |
| **Entra ID P1** or higher in the tenant, only if AD groups will hold the portal's roles | Giving an app's roles to a group needs P1. Without it, name the people instead (section 1.5). |
| **Teams administrator** | Allowing the OmniFile Teams app for your users. |
| **Exchange administrator** | Letting the service send fallback email from one mailbox, `OMNI_NOTIFY_SENDER` (section 8.2). |
| **Network:** whoever runs your ExpressRoute or VPN and the hub network | Peering the service's network to the hub (section 6). |
| **DNS:** an admin of your on-premises DNS servers | Conditional forwarders (section 6). |
| **AD:** a domain admin, once, and an account that may join computers to the domain | POSIX attributes (section 1.4) and the filer's join (section 12). |
| **Hypervisor:** an admin of Hyper-V, vSphere or KVM | The edge filer's VM (section 11). |

If you will sync an existing NAS directory with an annex and let recalled files go back there (an "Annex", `docs/design/annex.md`), your NAS administrator also gives the annex's restore account its rights on that NAS (`annex.md` §5). Where a return rule may replace a file at its origin (overwrite, keeping a version), that account also needs the right to delete files in those folders: the replace hides the old file by a rename and deletes it after the grace period. Without it the NAS refuses the hide, nothing is replaced, and the recalled file takes a `-restored` name.

### 1.2 Networks

- Your data centre reaches Azure over **ExpressRoute or a site-to-site VPN**, through a hub virtual
  network. The service's own network is peered to that hub (section 6). OmniFile reaches Azure
  storage only through private endpoints (decision 22), so this path carries every byte.
- The service uses `10.60.0.0/16` in Azure by default. If that collides with any of your networks,
  choose another range in `omnifile.env` (`OMNI_VNET_PREFIX` and the four subnets).
- The edge filer accepts, inbound, only: SSH (TCP 22, its AD admins), SMB (TCP 445), NFS (TCP 2049),
  and for NFSv3 rpcbind (111), MOUNT (20048), NLM locks (32803) and statd (32765), each TCP and UDP.
  Open these from your users' networks to it.
- The edge filer needs: your domain controllers (DNS, Kerberos, LDAP), your time source, and HTTPS
  (443 and 8443) to the service's network in Azure. Nothing on the internet.
- Your admin workstation needs the same reach into the service's network, from a range you list
  as `OMNI_ADMIN_CIDRS`: it sets up the database and Key Vault, which have no public address.
- Over ExpressRoute, private-endpoint traffic goes through the ExpressRoute gateway (1, 2 or 10 Gbps
  by its size), and Microsoft says it "might be reduced by half". Size the gateway for the data you
  will move. Private Link also charges about $0.01 per GB.

### 1.3 Your admin workstation

A Linux or macOS machine (Windows works through WSL) with:

- the Azure CLI 2.60 or later (`az`), with Bicep (`az bicep install`);
- `jq`, `curl`, `ssh`, and `git`;
- OpenSSL 3 or later (`openssl version` says `OpenSSL 3...` or `OpenSSL 4...`): the steps make an
  Ed25519 key and check TLS names, which macOS's own LibreSSL can't. On macOS, `brew install openssl`
  and put `$(brew --prefix openssl)/bin` first in your `PATH`;
- the PostgreSQL client 16 or later (`psql`);
- Python 3.12 or later and `uv` (to run the catalog migrations from the release);
- reach into Azure as described above.

**The release folder.** OmniFile gives you each release as a folder: the source of that release
(`deploy/`, `docs/`, `service/` and the rest). Every workstation step runs from the root of that
folder. Put the edge appliance files that come with the release (section 11) in a folder named
`appliance-files` inside it.

### 1.4 Active Directory: POSIX attributes

The edge filer and every Linux client must agree on each person's and each group's numeric ids.
OmniFile takes them from AD: the RFC 2307 attributes `uidNumber` (users), `gidNumber` (users'
primary group and every group), and `loginShell` (the filer's administrators). They are part of
the standard AD schema since Windows Server 2003 R2.

What needs them:

| Who | Attributes |
|---|---|
| Everyone who uses a share | `uidNumber` |
| **Domain Users** (everyone's primary group) | `gidNumber` |
| Every group a share or the filer names: the filer's admin group, and each share's Owners, Writers and Readers | `gidNumber` |
| The filer's administrators | `loginShell` (`/bin/bash`), so they can log in over ssh. Share users get no shell. |

The numbers must be inside **10000 to 999999** (the filer's id range) and unique. Once a number is
in use, **never change it**: files on the filer and on NFS clients carry the number, not the name.

The filer maps an AD user or group it can't give a number to as no one: a share whose Owners group
has no `gidNumber` isn't served, and the setup wizard stops if the admin group has none.

First, see what is missing. Replace each `<value:…>` below with your group names (the ones you will
also write in `omnifile.env`, section 2), and `Domain Users` with its name if your AD is in another
language. This lists the users and groups that lack attributes:

```powershell guide-step=1 on=ad-admin
# In the release folder's docs/guide/tools, on a machine with the ActiveDirectory module (RSAT).
.\Set-OmniFilePosixIds.ps1 -Report `
  -Groups 'Domain Users', '<value:OMNI_AD_ADMIN_GROUP>', '<value:OMNI_SHARE_OWNERS>', '<value:OMNI_SHARE_WRITERS>', '<value:OMNI_SHARE_READERS>' `
  -LoginShellGroup '<value:OMNI_AD_ADMIN_GROUP>'
```

It prints one line per user or group that lacks something, and changes nothing. The members of the
listed groups (including nested groups) are the users it checks.

**If your AD already has these attributes** (for example from an earlier Unix integration), and
the report says nothing is missing, go to section 1.5.

**If your AD has none, or some are missing**, the same script can hand out numbers. It never
changes a number that is already set, and it never reuses one: it starts above the highest number
already in use, or at the first numbers you give, whichever is higher. First a dry run:

```powershell guide-step=2 on=ad-admin
.\Set-OmniFilePosixIds.ps1 `
  -Groups 'Domain Users', '<value:OMNI_AD_ADMIN_GROUP>', '<value:OMNI_SHARE_OWNERS>', '<value:OMNI_SHARE_WRITERS>', '<value:OMNI_SHARE_READERS>' `
  -LoginShellGroup '<value:OMNI_AD_ADMIN_GROUP>' -FirstUid <value:OMNI_FIRST_UID> -FirstGid <value:OMNI_FIRST_GID>
```

It prints each change it would make (`would set uidNumber 20000 on bob.writer`). Read the list. If
it is right, apply it:

```powershell guide-step=3 on=ad-admin
.\Set-OmniFilePosixIds.ps1 `
  -Groups 'Domain Users', '<value:OMNI_AD_ADMIN_GROUP>', '<value:OMNI_SHARE_OWNERS>', '<value:OMNI_SHARE_WRITERS>', '<value:OMNI_SHARE_READERS>' `
  -LoginShellGroup '<value:OMNI_AD_ADMIN_GROUP>' -FirstUid <value:OMNI_FIRST_UID> -FirstGid <value:OMNI_FIRST_GID> -Apply
```

Success: it ends with `done: N changes`, and step 1 run again reports nothing missing. New people
need the attributes too: run step 3 again when you add users to these groups, or give the
attributes in your joiner process.

Users also need a `gidNumber` of their own only if your Linux clients use it as the primary group;
the filer uses Domain Users (the AD primary group), so the script doesn't set one.

### 1.5 AD groups and Entra Connect

Create these AD security groups (any names; write them in `omnifile.env`):

- **the filer's administrators** (`OMNI_AD_ADMIN_GROUP`): they get sudo and ssh on the filer;
- **the portal's roles**, if groups will hold them (below): ArchiveAdmin (`OMNI_GROUP_ARCHIVEADMIN`:
  policies, shares, WORM, audit), DataSteward (`OMNI_GROUP_DATASTEWARD`: tier pins and holds), and
  User (`OMNI_GROUP_USER`: everyone who uses the portal);
- **each share's** Owners, Writers and Readers.

**Who holds the portal's roles** is your choice now, in `omnifile.env` (`OMNI_ROLE_HOLDERS`):

| | `groups` | `people` |
|---|---|---|
| ArchiveAdmin and DataSteward | the AD groups above | the people you name in `OMNI_PEOPLE_ARCHIVEADMIN` and `OMNI_PEOPLE_DATASTEWARD` (sign-in names, the UPNs Entra shows, separated by spaces) |
| User | the AD group `OMNI_GROUP_USER` | every AD user in your tenant, with no assignment |
| Adding someone later | add them to the AD group | an Entra admin runs step 30 again with the new name, or assigns the role in Entra (Enterprise applications, the portal's app, Users and groups) |
| Needs | Entra ID P1 | nothing more than Entra ID Free, and the portal's app with "Assignment required" off (the default; step 30 checks it) |

Either way, what a person sees in the portal is limited by their AD permissions on each file: a role
decides what they may do, never which files they may see. With `people`, "every AD user" means an
account Entra Connect synced from your AD; a guest or a cloud-only account gets User only if you name it
as an ArchiveAdmin or DataSteward. You can change your mind later: edit `omnifile.env`, run step 30, then
deploy again (section 9.3). Switching doesn't take the roles away from the other mode's holders: remove
those in Entra (Enterprise applications, the portal's app, Users and groups).

Entra Connect must sync these groups and their members to Entra, with `onPremisesSecurityIdentifier`
(the default). That is how OmniFile matches an AD user opening a file to the person it sends a
Teams card to.

### 1.6 The edge filer's VM

| Item | Size |
|---|---|
| vCPUs | 4 |
| Memory | 8 GiB, fixed (not dynamic) |
| System disk | the appliance's disk, grown to 64 GiB before the first start (the steps do this) |
| Data disk | a second, empty disk: the local cache the shares live on (`OMNI_DATA_DISK_GB`) |
| TPM | a virtual TPM 2.0. The filer seals its keys to it. Without one it uses a key on its own disk. |
| Firmware | UEFI. Secure Boot off, unless you enrol OmniFile's signing key first (`docs/design/secure-boot.md`). |
| Network | one adapter, on the network your users reach the shares from, with a fixed address |

### 1.7 What OmniFile gives you

- The release folder (section 1.3), including `deploy/images/images.lock.json`, which names each
  service image by its digest.
- The registry the service images are published to (`OMNI_SOURCE_REGISTRY`, in the release notes).
- The appliance files for the edge filer, in `appliance-files/`: `omnifile-edge_<release>.vhdx`
  (Hyper-V), `omnifile-edge_<release>.ova` (vSphere), `omnifile-edge_<release>.qcow2` (KVM), and a
  `SHA256SUMS` file that lists them.

## 2. Your values

Make your values file. This copies the example the first time, and leaves your file alone after
that:

```bash guide-step=4 on=workstation
[ -f omnifile.env ] || cp docs/guide/omnifile.env.example omnifile.env
touch omnifile.state.env
echo "now edit omnifile.env"
```

Open `omnifile.env` in an editor and fill in every value. Each one has a comment that says what it
is. Then check them:

```bash guide-step=5 on=workstation
. ./omnifile.env && . ./omnifile.state.env
docs/guide/tools/check-env.sh
```

Success: `omnifile.env: every value is set and has the right shape`. Otherwise it lists each value
to fix.

Check the workstation's tools:

```bash guide-step=6 on=workstation
. ./omnifile.env && . ./omnifile.state.env
for tool in az jq openssl curl ssh psql uv python3; do
  command -v "$tool" >/dev/null || { echo "missing: $tool"; exit 1; }
done
az version --query '"azure-cli"' --output tsv
az bicep version
psql --version
python3 -c 'import sys; assert sys.version_info >= (3, 12), "Python 3.12 or later is needed"'
openssl version | grep -Eq '^OpenSSL ([3-9]|[1-9][0-9]+)\.' || { echo "openssl is $(openssl version): OpenSSL 3 or later is needed (section 1.3)"; exit 1; }
```

Success: no `missing:` line, an Azure CLI version of 2.60 or later, a Bicep version, psql 16 or
later, and no complaint about OpenSSL.

Check the appliance files are the ones OmniFile published:

```bash guide-step=7 on=workstation
. ./omnifile.env && . ./omnifile.state.env
cd appliance-files
if command -v sha256sum >/dev/null; then sha256sum --check SHA256SUMS; else shasum -a 256 --check SHA256SUMS; fi
```

Success: each file says `OK`.

## 3. Sign in to Azure

```bash guide-step=8 on=workstation manual=yes
. ./omnifile.env && . ./omnifile.state.env
az login --tenant "$OMNI_TENANT_ID"
az account set --subscription "$OMNI_SUBSCRIPTION_ID"
az account show --query '{subscription:name, tenant:tenantId, you:user.name}' --output table
```

A browser opens for you to sign in. Success: the table shows your subscription, tenant and name.

Record your own object id (the deployment lets you upload secrets to Key Vault later), the database
administrators' group, and your registry:

```bash guide-step=9 on=workstation plan=yes
. ./omnifile.env && . ./omnifile.state.env
me=$(az ad signed-in-user show --query id --output tsv)
docs/guide/tools/state.sh set OMNI_ADMIN_OBJECT_ID "$me"
group=$(az ad group show --group "$OMNI_PG_ADMIN_GROUP" --query id --output tsv)
docs/guide/tools/state.sh set OMNI_PG_ADMIN_GROUP_ID "$group"
member=$(az ad group member check --group "$group" --member-id "$me" --query value --output tsv)
[ "$member" = true ] || { echo "you are not a member of $OMNI_PG_ADMIN_GROUP: add yourself, then run this step again"; exit 1; }
docs/guide/tools/state.sh set OMNI_ACR_ID "$(az acr show --name "$OMNI_ACR" --query id --output tsv)"
```

Success: three `recorded` lines. If it says you are not a member of the database group, add
yourself in Entra and wait a few minutes.

Create the resource group:

```bash guide-step=10 on=workstation
. ./omnifile.env && . ./omnifile.state.env
tags=()
if [ -n "${OMNI_TEST_RUN_ID:-}" ]; then
  tags=(--tags "omnifile-run=$OMNI_TEST_RUN_ID" "omnifile-expires=$OMNI_TEST_RUN_EXPIRES" omnifile-harness=tests-real)
fi
if az group show --name "$OMNI_RG" --output none 2>/dev/null; then
  echo "the resource group $OMNI_RG is there already: left as it is"
else
  az group create --name "$OMNI_RG" --location "$OMNI_LOCATION" ${tags[@]+"${tags[@]}"} --output table
fi
```

Success: the group, in `Succeeded` state (or the note that it is there already: a group that exists
keeps its settings and tags).

## 4. Images

The service runs as containers in Azure Container Apps. Copy OmniFile's images into your registry,
by digest, so that what runs is exactly what was released and tested:

```bash guide-step=11 on=workstation
. ./omnifile.env && . ./omnifile.state.env
lock=deploy/images/images.lock.json
for image in $(jq -r '.images | keys[]' "$lock"); do
  repo=$(jq -r --arg i "$image" '.images[$i].repository' "$lock")
  digest=$(jq -r --arg i "$image" '.images[$i].digest' "$lock")
  az acr import --name "$OMNI_ACR" --source "$OMNI_SOURCE_REGISTRY/$repo@$digest" \
    --image "$repo:$OMNI_RELEASE" --force
  echo "imported $repo@$digest"
done
```

Success: one `imported` line per image (three: the service, the portal and the relay). Then check
that each digest is in your registry:

```bash guide-step=12 on=workstation
. ./omnifile.env && . ./omnifile.state.env
lock=deploy/images/images.lock.json
for image in $(jq -r '.images | keys[]' "$lock"); do
  repo=$(jq -r --arg i "$image" '.images[$i].repository' "$lock")
  digest=$(jq -r --arg i "$image" '.images[$i].digest' "$lock")
  az acr repository show --name "$OMNI_ACR" --image "$repo@$digest" --query digest --output tsv
done
```

Success: each digest printed, the same as in the lock file. The deployment refers to the images by
these digests (`docs/guide/customer.bicepparam` reads the lock file), never by a tag.

## 5. Deploy the service: phase 1

The deployment is in two phases. Phase 1 creates everything: the network, the database, storage, Key
Vault, the container apps and their identities. Phase 2 (section 9) adds what only exists once phase
1 has run: the edge CA certificate, the portal's app registration, and the secrets the apps share.
Both use the same parameters file, `docs/guide/customer.bicepparam`, which reads `omnifile.env` and
`omnifile.state.env`; you don't edit it.

`OMNI_DEPLOYMENT_SIZE` in `omnifile.env` picks the size (`deploymentSize`):

- **standard** (the default): about $680 a month at list prices in West US 3, before storage and
  data. About $275 is the PostgreSQL server and about $280 the apps; the rest is the private
  endpoints, the environments' load balancers, logs, alerts and the vault's key.
- **small**, for a site with a few edges: about $185 a month. The private networking, identities
  and allow-lists are the same. The database server is a Burstable B1ms, and the apps are smaller,
  with fewer catalog connections each. `web` and `enrol` stop when idle, so the first portal page
  or enrolment after a quiet spell waits a few seconds for them to start. Logs are capped at 0.5 GB
  a day and kept 30 days, and the alerts run every 15 minutes.

Other regions cost differently. Every resource's price at both sizes is in
`docs/milestones/results/wp16.md`. You can go from small to standard later: set the value and run
sections 5.2 and 9.3 again. You can't go back, because a database server's storage can't shrink.

### 5.1 The relay's allowed addresses

The Teams relay is the only public endpoint. It admits only Microsoft's Bot Service addresses
(decision 49), which this takes from Azure's published service tag:

```bash guide-step=13 on=workstation plan=yes
. ./omnifile.env && . ./omnifile.state.env
deploy/azure/relay-ranges.sh --location "$OMNI_LOCATION" --out deploy/azure/relay-allowed-cidrs.json
```

Success: it prints how many IPv4 prefixes it wrote. Run it again before every deployment: Microsoft
changes the list.

### 5.2 Preview, then deploy

First a preview of what the deployment will do (Azure's what-if):

```bash guide-step=14 on=workstation gate=whatif
. ./omnifile.env && . ./omnifile.state.env
deploy/azure/whatif.sh -g "$OMNI_RG" -p docs/guide/customer.bicepparam | tee whatif-phase1.txt
docs/guide/tools/state.sh set OMNI_WHATIF_DIGEST "$(tail -n 1 whatif-phase1.txt | sed 's/^digest: //')"
```

Read `whatif-phase1.txt`. In a new resource group every change is a **Create**. Its last line is
`digest: sha256:…`, a fingerprint of exactly this preview, which the step records.

Deploy what you previewed. This refuses to run if the preview would now be different:

```bash guide-step=15 on=workstation
. ./omnifile.env && . ./omnifile.state.env
deploy/azure/whatif.sh -g "$OMNI_RG" -p docs/guide/customer.bicepparam --deploy --approve "$OMNI_WHATIF_DIGEST"
```

It takes about 30 to 45 minutes (PostgreSQL is the slowest part). Success: it ends with the
deployment `Succeeded`. Record its outputs:

```bash guide-step=16 on=workstation
. ./omnifile.env && . ./omnifile.state.env
docs/guide/tools/outputs.sh --resource-group "$OMNI_RG"
```

Success: a list of recorded values (the service's addresses, the database, Key Vault, the storage
accounts), and `pe-targets.json` with the private endpoints to create.

### 5.3 What phase 1 looks like

```bash guide-step=17 on=workstation
. ./omnifile.env && . ./omnifile.state.env
az containerapp list --resource-group "$OMNI_RG" \
  --query '[].{app:name, status:properties.runningStatus}' --output table
```

Expected after phase 1: every app is `Running`, and `api`, `enrol`, `web` and the relay are not ready
yet. Each one's health check answers `503` with what it is waiting for, and its log says so in one
line: the edge CA certificate (section 8.3), the portal's app registration (section 8.2), the shared
secrets (section 9.2). They become ready in phase 2. The `worker` runs. At the small size, `web` and
`enrol` may have no replica running yet: each one starts at its first request. To see why an app waits:
`az containerapp logs show --resource-group "$OMNI_RG" --name "$OMNI_PREFIX-web" --tail 20`.

The storage accounts and Key Vault have no public access, so nothing can use them until the next
section is done.

## 6. Private endpoints and DNS

OmniFile reaches storage and Key Vault only through private endpoints, which you create (decision
22). Then your on-premises network has to find them by name.

### 6.1 The private DNS zones

Two zones hold the private addresses: `privatelink.blob.core.windows.net` (storage) and
`privatelink.vaultcore.azure.net` (Key Vault). They are linked to the service's network and to your
hub's, so that your DNS resolver in the hub answers for them too. Two zones the deployment made are
linked to the hub as well: the internal environment's (the apps' names) and the database's
(`<server>.private.postgres.database.azure.com`).

If your hub already has the two `privatelink` zones, set `OMNI_DNS_ZONE_RG` to their resource group
and this step uses them (you need rights to add links there):

```bash guide-step=18 on=workstation
. ./omnifile.env && . ./omnifile.state.env
zone_rg=${OMNI_DNS_ZONE_RG:-$OMNI_RG}
for zone in $(jq -r '.[]' dns-zones.json); do
  az network private-dns zone show --resource-group "$zone_rg" --name "$zone" --output none 2>/dev/null ||
    az network private-dns zone create --resource-group "$zone_rg" --name "$zone" --output none
  for vnet in "$OMNI_VNET_ID" "$OMNI_HUB_VNET_ID"; do
    link="link-$(basename "$vnet")"
    az network private-dns link vnet show --resource-group "$zone_rg" --zone-name "$zone" --name "$link" --output none 2>/dev/null ||
      az network private-dns link vnet create --resource-group "$zone_rg" --zone-name "$zone" --name "$link" \
        --virtual-network "$vnet" --registration-enabled false --output none
  done
  echo "zone $zone linked to the service's network and the hub"
done
link="link-$(basename "$OMNI_HUB_VNET_ID")"
az network private-dns link vnet show --resource-group "$OMNI_RG" --zone-name "$OMNI_INTERNAL_DOMAIN" --name "$link" --output none 2>/dev/null ||
  az network private-dns link vnet create --resource-group "$OMNI_RG" --zone-name "$OMNI_INTERNAL_DOMAIN" --name "$link" \
    --virtual-network "$OMNI_HUB_VNET_ID" --registration-enabled false --output none
echo "zone $OMNI_INTERNAL_DOMAIN linked to the hub"
pg_zone="${OMNI_PG_HOST%%.*}.private.postgres.database.azure.com"
az network private-dns link vnet show --resource-group "$OMNI_RG" --zone-name "$pg_zone" --name "$link" --output none 2>/dev/null ||
  az network private-dns link vnet create --resource-group "$OMNI_RG" --zone-name "$pg_zone" --name "$link" \
    --virtual-network "$OMNI_HUB_VNET_ID" --registration-enabled false --output none
echo "zone $pg_zone linked to the hub"
```

Success: four `linked` lines.

### 6.2 The private endpoints

One for each storage account and one for Key Vault, in the service's private-endpoint subnet, each
registered in its zone:

```bash guide-step=19 on=workstation
. ./omnifile.env && . ./omnifile.state.env
zone_rg=${OMNI_DNS_ZONE_RG:-$OMNI_RG}
jq -c '.[]' pe-targets.json | while read -r target; do
  id=$(jq -r .resourceId <<<"$target")
  group=$(jq -r .groupId <<<"$target")
  zone=$(jq -r .privateDnsZone <<<"$target")
  name="pe-$(basename "$id")-$group"
  az network private-endpoint show --resource-group "$OMNI_RG" --name "$name" --output none 2>/dev/null ||
    az network private-endpoint create --resource-group "$OMNI_RG" --name "$name" --location "$OMNI_LOCATION" \
      --subnet "$OMNI_PE_SUBNET_ID" --private-connection-resource-id "$id" --group-id "$group" \
      --connection-name "$name" --output none
  zone_id=$(az network private-dns zone show --resource-group "$zone_rg" --name "$zone" --query id --output tsv)
  az network private-endpoint dns-zone-group show --resource-group "$OMNI_RG" --endpoint-name "$name" --name default --output none 2>/dev/null ||
    az network private-endpoint dns-zone-group create --resource-group "$OMNI_RG" --endpoint-name "$name" --name default \
      --private-dns-zone "$zone_id" --zone-name "${zone//./-}" --output none
  echo "$name: $(az network private-endpoint show --resource-group "$OMNI_RG" --name "$name" \
    --query 'customDnsConfigs[0].ipAddresses[0]' --output tsv)"
done
```

Success: one line per endpoint with its private address, inside `OMNI_SUBNET_PE`.

### 6.3 Peer the service's network to your hub

Your network team may prefer to do this themselves; this is what it needs. The service's network
uses the hub's gateway to reach your data centre:

```bash guide-step=20 on=workstation
. ./omnifile.env && . ./omnifile.state.env
hub_sub=$(cut -d/ -f3 <<<"$OMNI_HUB_VNET_ID")
hub_rg=$(cut -d/ -f5 <<<"$OMNI_HUB_VNET_ID")
hub_name=$(basename "$OMNI_HUB_VNET_ID")
# Gateway transit only when the hub has a gateway (its GatewaySubnet holds the gateway's IP configuration).
gw=$(az network vnet show --ids "$OMNI_HUB_VNET_ID" \
  --query "subnets[?name=='GatewaySubnet'].ipConfigurations[].id | length(@)" --output tsv)
if [ "${gw:-0}" -gt 0 ]; then hub_flag=--allow-gateway-transit; spoke_flag=--use-remote-gateways
elif [ "${OMNI_TEST_HUB_NO_GATEWAY:-}" = 1 ]; then
  hub_flag='' spoke_flag=''; echo "test run (OMNI_TEST_HUB_NO_GATEWAY=1): a hub with no gateway, peered without transit"
else
  echo "the hub $hub_name has no ExpressRoute or VPN gateway: your data centre could not reach the service."
  echo "Name the hub that holds your gateway in OMNI_HUB_VNET_ID, then run this step again."
  exit 1
fi
az network vnet peering show --resource-group "$hub_rg" --vnet-name "$hub_name" --name "to-$OMNI_PREFIX-vnet" \
  --subscription "$hub_sub" --output none 2>/dev/null ||
  az network vnet peering create --resource-group "$hub_rg" --vnet-name "$hub_name" --name "to-$OMNI_PREFIX-vnet" \
    --subscription "$hub_sub" --remote-vnet "$OMNI_VNET_ID" --allow-vnet-access ${hub_flag:+"$hub_flag"} --output none
az network vnet peering show --resource-group "$OMNI_RG" --vnet-name "$OMNI_PREFIX-vnet" --name to-hub --output none 2>/dev/null ||
  az network vnet peering create --resource-group "$OMNI_RG" --vnet-name "$OMNI_PREFIX-vnet" --name to-hub \
    --remote-vnet "$OMNI_HUB_VNET_ID" --allow-vnet-access ${spoke_flag:+"$spoke_flag"} --output none
az network vnet peering list --resource-group "$OMNI_RG" --vnet-name "$OMNI_PREFIX-vnet" \
  --query '[].{peering:name, state:peeringState}' --output table
```

Success: `to-hub` is `Connected`, and the service's network uses the hub's gateway. The step stops if
the hub has no gateway, since the filer could then never reach the private endpoints. If your gateway
advertises routes to on-premises by BGP, the
service's range now reaches your data centre; if you route statically, add `OMNI_VNET_PREFIX` to the
routes towards Azure.

### 6.4 Your on-premises DNS

Your DNS servers forward four zones to the resolver in your hub (`OMNI_DNS_RESOLVER_IP`): the apps'
names, storage, Key Vault, and `postgres.database.azure.com` for the database. (The database's name
is public, `<server>.postgres.database.azure.com`, and points to its private zone, which only the
resolver in Azure can answer. With the forwarder, every Azure PostgreSQL name your DNS servers look up
goes through that resolver, which still answers public servers as before.) This writes the exact
commands for a Windows DNS server into `dns-forwarders.ps1`:

```bash guide-step=21 on=workstation
. ./omnifile.env && . ./omnifile.state.env
{
  for zone in "$OMNI_INTERNAL_DOMAIN" privatelink.blob.core.windows.net privatelink.vaultcore.azure.net \
    postgres.database.azure.com; do
    printf "if (-not (Get-DnsServerZone -Name '%s' -ErrorAction SilentlyContinue)) {\n" "$zone"
    printf "  Add-DnsServerConditionalForwarderZone -Name '%s' -MasterServers '%s' -ReplicationScope Forest\n}\n" \
      "$zone" "$OMNI_DNS_RESOLVER_IP"
  done
} >dns-forwarders.ps1
cat dns-forwarders.ps1
```

Copy `dns-forwarders.ps1` to one of your DNS servers (AD-integrated, so it replicates to the others)
and run it there:

```powershell guide-step=22 on=dns-server manual=yes
.\dns-forwarders.ps1
Get-DnsServerZone | Where-Object ZoneType -eq 'Forwarder' | Format-Table ZoneName, MasterServers
```

Success: the four zones listed as forwarders. If you use another DNS product, make the same four
conditional forwarders there.

### 6.5 Check that names resolve to private addresses

From the workstation, which uses your on-premises DNS:

```bash guide-step=23 on=workstation
. ./omnifile.env && . ./omnifile.state.env
docs/guide/tools/check-private-dns.sh \
  "$OMNI_LIVE_ACCOUNT.blob.core.windows.net" "$OMNI_ARCHIVE_ACCOUNT.blob.core.windows.net" \
  "$OMNI_KV_NAME.vault.azure.net" "${OMNI_ENROL_URL#https://}" "${OMNI_WEB_URL#https://}" \
  "${OMNI_API_URL#https://}" "$OMNI_PG_HOST"
```

Success: every name resolves to a private address (`10.`, `172.16-31.` or `192.168.`), and it ends
with `all private`. A name that resolves to a public address means a zone or forwarder is missing:
the edge would refuse it (it never falls back to a public path).

## 7. The database

The catalog lives in Azure Database for PostgreSQL, which accepts only Entra sign-in. You sign in as
a member of the database administrators' group, with a token from `az`.

### 7.1 Database roles for the service's identities

Each app signs in to the database as its own managed identity. Create a role for each:

```bash guide-step=24 on=workstation
. ./omnifile.env && . ./omnifile.state.env
PGPASSWORD=$(az account get-access-token --resource-type oss-rdbms --query accessToken --output tsv) \
  psql "host=$OMNI_PG_HOST dbname=postgres user='$OMNI_PG_ADMIN_GROUP' sslmode=verify-full sslrootcert=system" \
  -v prefix="$OMNI_PREFIX" -f docs/guide/sql/create-principals.sql
```

Success: five notices, `created role id-<prefix>-api` and so on (or `already exists` on a rerun).

### 7.2 The catalog

Install the catalog's tables, its work queues and each app's privileges. This runs the release's
own migrations, from the release folder:

```bash guide-step=25 on=workstation
. ./omnifile.env && . ./omnifile.state.env
members="api=id-$OMNI_PREFIX-api,enrol=id-$OMNI_PREFIX-enrol,web=id-$OMNI_PREFIX-web"
members="$members,worker=id-$OMNI_PREFIX-worker,worker=id-$OMNI_PREFIX-verifier"
PGPASSWORD=$(az account get-access-token --resource-type oss-rdbms --query accessToken --output tsv) \
OMNIFILE_DATABASE_URL="host=$OMNI_PG_HOST dbname=$OMNI_PG_DATABASE user='$OMNI_PG_ADMIN_GROUP' sslmode=verify-full sslrootcert=system" \
OMNIFILE_DB_MEMBERS="$members" \
  uv run --project service --no-dev python -m omnifile_service.catalog.migrate
```

Success: it lists each migration it applied and ends without an error. Run it again after every
upgrade: it applies only what is new, and gives new tables their privileges.

### 7.3 Register the storage accounts

Tell the catalog about the two storage accounts the deployment made (the non-WORM layout: a live
account with the Smart tier, and an archive account):

```bash guide-step=26 on=workstation
. ./omnifile.env && . ./omnifile.state.env
PGPASSWORD=$(az account get-access-token --resource-type oss-rdbms --query accessToken --output tsv) \
OMNIFILE_DATABASE_URL="host=$OMNI_PG_HOST dbname=$OMNI_PG_DATABASE user='$OMNI_PG_ADMIN_GROUP' sslmode=verify-full sslrootcert=system" \
  uv run --project service --no-dev python -m omnifile_service.admin backend add azure \
  --live "$OMNI_LIVE_ACCOUNT" --archive "$OMNI_ARCHIVE_ACCOUNT" --region "$OMNI_LOCATION"
```

Success: one line, `added: azure (backend 1, azure_blob, private network): live <live account>,
archive <archive account>, Smart, <region>`, or `unchanged: ...` on a rerun. The portal offers it as
"azure" when you add a share. The api and worker read it when phase 2 deploys them (section 9).

## 8. The portal's app registration, Teams, and the edge CA

### 8.1 The Teams app

The deployment made the Azure Bot that sends the Teams cards (its app id is `teamsBotAppId`). Build
the Teams app package that names it:

```bash guide-step=27 on=workstation
. ./omnifile.env && . ./omnifile.state.env
deploy/teams/build.sh --bot-app-id "$OMNI_BOT_APP_ID" --portal-url "$OMNI_WEB_URL" --version "$OMNI_RELEASE" \
  --relay-host "${OMNI_RELAY_URL#https://}"
ls -l deploy/teams/out/omnifile-teams-app.zip
```

Success: `deploy/teams/out/omnifile-teams-app.zip` exists.

### 8.2 The portal's app registration

This creates the portal's app registration in Entra (its roles, its sign-in address, and a
federated credential for the portal's managed identity, so there is no client secret), gives the
service's worker identity the Microsoft Graph permissions it needs to find people and send them
cards (`User.Read.All`, `GroupMember.Read.All`, `TeamsAppInstallation.ReadWriteSelfForUser.All`: this
is the admin consent), and publishes the Teams app to your organisation's catalog. Email is the
fallback when Teams can't reach someone. The worker sends it from one mailbox, `OMNI_NOTIFY_SENDER`,
with `Mail.Send` scoped to that mailbox in Exchange Online (the next step), never tenant-wide:

```bash guide-step=28 on=workstation
. ./omnifile.env && . ./omnifile.state.env
worker_client=$(az identity show --resource-group "$OMNI_RG" --name "id-$OMNI_PREFIX-worker" --query clientId --output tsv)
docs/guide/tools/state.sh set OMNI_WORKER_CLIENT_ID "$worker_client"
deploy/entra/register.sh --tenant "$OMNI_TENANT_ID" --prefix "$OMNI_PREFIX" --web-origin "$OMNI_WEB_URL" \
  --web-principal "$OMNI_WEB_PRINCIPAL_ID" --worker-principal "$OMNI_WORKER_PRINCIPAL_ID" \
  --teams-app-zip deploy/teams/out/omnifile-teams-app.zip \
  --mail-sender "$OMNI_NOTIFY_SENDER" --worker-client-id "$worker_client" --out deploy/entra/out/entra.json
docs/guide/tools/state.sh set OMNI_WEB_CLIENT_ID "$(jq -r .webClientId deploy/entra/out/entra.json)"
docs/guide/tools/state.sh set OMNI_WEB_API_AUDIENCES "$(jq -c .webApiAudiences deploy/entra/out/entra.json)"
docs/guide/tools/state.sh set OMNI_TEAMS_APP_CATALOG_ID "$(jq -r .teamsAppCatalogId deploy/entra/out/entra.json)"
```

You need the Entra rights of section 1.1 for this step. Success: four `recorded` lines, and at the end
the Exchange Online steps for the sender mailbox, which are these. An Exchange administrator runs them
in Exchange Online PowerShell (the `ExchangeOnlineManagement` module):

```powershell guide-step=29 on=exchange-admin manual=yes
Connect-ExchangeOnline
$App = '<value:OMNI_WORKER_CLIENT_ID>'
$Scope = 'OmniFile <value:OMNI_PREFIX> sender'
if (-not (Get-ServicePrincipal -Identity $App -ErrorAction SilentlyContinue)) {
  New-ServicePrincipal -AppId $App -ObjectId '<value:OMNI_WORKER_PRINCIPAL_ID>' -DisplayName 'OmniFile <value:OMNI_PREFIX> worker'
}
if (-not (Get-ManagementScope -Identity $Scope -ErrorAction SilentlyContinue)) {
  New-ManagementScope -Name $Scope -RecipientRestrictionFilter "PrimarySmtpAddress -eq '<value:OMNI_NOTIFY_SENDER>'"
}
if (-not (Get-ManagementRoleAssignment -RoleAssignee $App -Role 'Application Mail.Send' -ErrorAction SilentlyContinue)) {
  New-ManagementRoleAssignment -App $App -Role 'Application Mail.Send' -CustomResourceScope $Scope
}
Test-ServicePrincipalAuthorization -Identity $App -Resource '<value:OMNI_NOTIFY_SENDER>'
```


Each command runs only if what it makes isn't there yet, so the step is safe to repeat. Success: the last
command lists `Application Mail.Send` with `InScope` true for the sender mailbox.
The worker may send as that mailbox and no other. (The deployment passes `OMNI_NOTIFY_SENDER` to the
worker as `notifySender`.)

Give the portal's roles to the groups or the people you chose in section 1.5:

```bash guide-step=30 on=workstation
. ./omnifile.env && . ./omnifile.state.env
docs/guide/tools/assign-app-roles.sh --app-id "$OMNI_WEB_CLIENT_ID" --portal-roles
```

Success: `<group or person> now has <role>` for each (or `already has` on a rerun). With `groups` on a
tenant without Entra ID P1, it stops before assigning anything and says so: set
`OMNI_ROLE_HOLDERS=people` and name the people in `omnifile.env`, then run this step again. Phase 2's
deployment (section 9) gives the service the choice.

Allow the Teams app for your users. A Teams administrator does this in the Teams admin centre:

```text guide-step=31 on=browser manual=yes
open https://admin.teams.microsoft.com/policies/manage-apps
search OmniFile Gateway
click OmniFile Gateway
click Allow
```

Success: the app's status is **Allowed**. If your organisation limits apps by permission policy,
add it to the policy your OmniFile users have. OmniFile installs the app for each person itself the
first time it sends them a card.

### 8.3 The edge CA

Edges prove who they are with certificates from OmniFile's own certificate authority (CA). Its key
was made inside Key Vault by the deployment and can never leave it; only the `enrol` app may sign
with it. Make the CA's own certificate, once, by asking `enrol` to sign it:

```bash guide-step=32 on=workstation
. ./omnifile.env && . ./omnifile.state.env
if [ -s edge-ca.pem ]; then echo "edge-ca.pem is already here: the CA is made"; exit 0; fi
docs/guide/tools/enrol-exec.sh --resource-group "$OMNI_RG" --app "$OMNI_PREFIX-enrol" \
  --command "python -m omnifile_service.enrol init-kv-ca /tmp/edge-ca.pem" \
  --then "cat /tmp/edge-ca.pem" --pem-out edge-ca.pem
```

`enrol` prints `wrote /tmp/edge-ca.pem: CN=OmniFile edge CA, SHA-256 <fingerprint>`: write the
fingerprint down. Then the step prints the certificate and keeps it as `edge-ca.pem` in the release
folder. The certificate is public; it is not a secret. Both commands run in the same replica of
`enrol`, through `az containerapp exec`. If the step says it couldn't cut the certificate from the
output, copy it by hand: everything from `-----BEGIN CERTIFICATE-----` to `-----END
CERTIFICATE-----`, both lines included, into `edge-ca.pem`. Make the CA only once: a second
`init-kv-ca` makes a different certificate for the same key.

Check the copy and record it:

```bash guide-step=33 on=workstation
. ./omnifile.env && . ./omnifile.state.env
openssl x509 -in edge-ca.pem -noout -subject -enddate -fingerprint -sha256
docs/guide/tools/state.sh set-file OMNI_EDGE_CA_PEM edge-ca.pem
```

Success: the subject is `CN = OmniFile edge CA`, it lasts ten years, and the SHA-256 fingerprint is
the one step 32 printed (ignore the colons).

### 8.4 The relay's certificate

The relay passes Teams clicks to the private service over mutual TLS, with a certificate of its own
from the edge CA. Make its key and a certificate request on the workstation:

```bash guide-step=34 on=workstation
. ./omnifile.env && . ./omnifile.state.env
umask 077
openssl req -new -newkey ec -pkeyopt ec_paramgen_curve:P-256 -nodes -keyout relay.key \
  -subj /CN=omnifile-relay -out relay.csr
base64 <relay.csr | tr -d '\n' >relay.csr.b64
echo "relay.csr.b64: $(wc -c <relay.csr.b64) bytes"
```

Have `enrol` sign it:

```bash guide-step=35 on=workstation
. ./omnifile.env && . ./omnifile.state.env
docs/guide/tools/enrol-exec.sh --resource-group "$OMNI_RG" --app "$OMNI_PREFIX-enrol" \
  --command "python -m omnifile_service.enrol issue-relay-cert --csr-b64 $(cat relay.csr.b64)" --pem-out relay.pem
```

`enrol` prints the relay's certificate, then a line `SHA-256: <64 hex digits>`; the step keeps the
certificate as `relay.pem` (if it can't, copy it by hand, as in step 32). It lasts 180 days; section
16 says how to renew it. Check it and record its fingerprint, which `api` pins:

```bash guide-step=36 on=workstation
. ./omnifile.env && . ./omnifile.state.env
openssl verify -CAfile edge-ca.pem relay.pem
[ "$(openssl x509 -in relay.pem -noout -pubkey)" = "$(openssl pkey -in relay.key -pubout)" ] ||
  { echo "relay.pem is not for relay.key"; exit 1; }
docs/guide/tools/state.sh set OMNI_RELAY_CERT_SHA256 \
  "$(openssl x509 -in relay.pem -outform DER | openssl dgst -sha256 | awk '{print $NF}')"
```

Success: `relay.pem: OK`, and the recorded SHA-256 is the one step 35 printed.

### 8.5 The Teams card signing key

Every button on a Teams card carries a signed intent. The worker signs with a private key; the relay
and `api` check with its public half. Make the key pair:

```bash guide-step=37 on=workstation
. ./omnifile.env && . ./omnifile.state.env
if [ -n "${OMNI_INTENT_KID:-}" ]; then echo "the signing key $OMNI_INTENT_KID is made already: left as it is"; exit 0; fi
umask 077
[ -f intent.key ] || openssl genpkey -algorithm ed25519 -out intent.key
kid="intent-$(date -u +%Y%m%d)"
pub=$(openssl pkey -in intent.key -pubout -outform DER | tail -c 32 | base64 | tr '+/' '-_' | tr -d '=\n')
docs/guide/tools/state.sh set OMNI_INTENT_KID "$kid"
docs/guide/tools/state.sh set OMNI_INTENT_PUBLIC_KEYS "{\"$kid\":\"$pub\"}"
```

Success: two `recorded` lines. The key is made once: once its public half is recorded, the step leaves
it alone (a new key would no longer match the one Key Vault keeps, and every Teams button would stop
working).

## 9. Deploy the service: phase 2

### 9.1 The enrolment pin

An edge enrolling checks that it talks to your `enrol` app by the certificate authority that issued
`enrol`'s TLS certificate. Take that CA's fingerprint. `enrol` admits your workstation's network
(`OMNI_ADMIN_CIDRS`) as well as the edges':

```bash guide-step=38 on=workstation
. ./omnifile.env && . ./omnifile.state.env
docs/guide/tools/tls-issuer.sh --host "$OMNI_ENROL_URL" --out enrol-issuer.pem | tee enrol-issuer.txt
docs/guide/tools/state.sh set OMNI_ENROL_SERVER_CA_SHA256 "$(tail -n 1 enrol-issuer.txt)"
```

`tls-issuer.sh` checks the certificate `enrol` presents against the workstation's trusted CAs and its
name, and stops if either is wrong (an interception proxy, a wrong DNS answer), so a wrong pin is never
recorded. Success: the issuer's subject (Microsoft's, for the Container Apps environment), its SHA-256,
and `recorded OMNI_ENROL_SERVER_CA_SHA256`.

### 9.2 The secrets

Put the keys every replica of an app shares, and the relay's certificate, in Key Vault. The
deployment gave you the right to do so (Key Vault Secrets Officer on this vault only), and you reach
the vault through its private endpoint:

```bash guide-step=39 on=workstation
. ./omnifile.env && . ./omnifile.state.env
kv=$OMNI_KV_NAME
put_once() { # name, then the az arguments that give the value
  if az keyvault secret show --vault-name "$kv" --name "$1" --query id --output tsv >/dev/null 2>&1; then
    echo "$1 is already in the vault: left as it is"
  else
    az keyvault secret set --vault-name "$kv" --name "$@" --output none
    echo "$1 uploaded"
  fi
}
umask 077
fresh=$(mktemp)
trap 'rm -f "$fresh"' EXIT
openssl rand -base64 32 | tr -d '\n' >"$fresh"; put_once api-lease-key --file "$fresh" --encoding utf-8
openssl rand -base64 32 | tr -d '\n' >"$fresh"; put_once web-cursor-key --file "$fresh" --encoding utf-8
put_once teams-intent-signing-key --file intent.key --encoding utf-8
az keyvault secret set --vault-name "$kv" --name relay-mtls-cert --file relay.pem --encoding utf-8 --output none
az keyvault secret set --vault-name "$kv" --name relay-mtls-key --file relay.key --encoding utf-8 --output none
az keyvault secret set --vault-name "$kv" --name relay-ca-bundle --file edge-ca.pem --encoding utf-8 --output none
echo "relay-mtls-cert, relay-mtls-key and relay-ca-bundle uploaded"
docs/guide/tools/state.sh set OMNI_PHASE 2
```

Success: six `uploaded` (or `left as it is`) lines and `recorded OMNI_PHASE`. If it is refused with
`Forbidden`, the role assignment is still reaching the vault: wait five minutes and run it again.

The lease, cursor and signing keys are made once and kept: replacing one later signs everyone out
of the portal or voids the Teams cards already sent. Now delete the private keys from the
workstation. Key Vault holds the only copies:

```bash guide-step=40 on=workstation
. ./omnifile.env && . ./omnifile.state.env
rm -f relay.key intent.key relay.csr relay.csr.b64
ls relay.pem edge-ca.pem
```

### 9.3 Preview and deploy

```bash guide-step=41 on=workstation gate=whatif
. ./omnifile.env && . ./omnifile.state.env
deploy/azure/relay-ranges.sh --location "$OMNI_LOCATION" --out deploy/azure/relay-allowed-cidrs.json
deploy/azure/whatif.sh -g "$OMNI_RG" -p docs/guide/customer.bicepparam | tee whatif-phase2.txt
docs/guide/tools/state.sh set OMNI_WHATIF_DIGEST "$(tail -n 1 whatif-phase2.txt | sed 's/^digest: //')"
```

Read `whatif-phase2.txt`. Expect **Modify** on the container apps (new settings and secrets) and
**Create** for role assignments on the new secrets. Nothing should be **Delete**: if something is,
stop and ask OmniFile.

```bash guide-step=42 on=workstation
. ./omnifile.env && . ./omnifile.state.env
deploy/azure/whatif.sh -g "$OMNI_RG" -p docs/guide/customer.bicepparam --deploy --approve "$OMNI_WHATIF_DIGEST"
docs/guide/tools/outputs.sh --resource-group "$OMNI_RG"
```

Success: the deployment `Succeeded`, and the outputs are recorded again.

Now give up your right to the vault's secrets. Key Vault Secrets Officer could read the relay's key and
the Teams signing key, and you no longer need it: the apps read their own secrets. Phase 2 no longer
grants it (`customer.bicepparam` names no uploader once `OMNI_PHASE` is 2), and this removes the grant
phase 1 made:

```bash guide-step=43 on=workstation
. ./omnifile.env && . ./omnifile.state.env
kv_id=$(az keyvault show --name "$OMNI_KV_NAME" --query id --output tsv)
have=$(az role assignment list --assignee "$OMNI_ADMIN_OBJECT_ID" --role "Key Vault Secrets Officer" --scope "$kv_id" \
  --query '[].id' --output tsv)
[ -z "$have" ] || az role assignment delete --assignee "$OMNI_ADMIN_OBJECT_ID" --role "Key Vault Secrets Officer" --scope "$kv_id"
echo "Key Vault Secrets Officer on $OMNI_KV_NAME: removed from you"
```

### 9.4 Check the service

```bash guide-step=44 on=workstation
. ./omnifile.env && . ./omnifile.state.env
az containerapp list --resource-group "$OMNI_RG" \
  --query '[].{app:name, status:properties.runningStatus}' --output table
code=$(curl --silent --output /dev/null --write-out '%{http_code}' "$OMNI_RELAY_URL/healthz" || true)
echo "relay from this workstation: $code (refused is right: it admits only Microsoft's Bot Service)"
```

Success: every app `Running`, and the relay refuses you (`403`, or no answer). The portal and `enrol`
admit your workstation's network:

```bash guide-step=45 on=workstation
. ./omnifile.env && . ./omnifile.state.env
curl --silent --show-error --fail "$OMNI_WEB_URL/healthz"; echo
curl --silent --show-error --fail "$OMNI_ENROL_URL/healthz"; echo
```

Success: both answer. `api` admits only the edges (and the relay), so check it from the edges' network,
with a copy of `edge-ca.pem` (public) in the release folder there:

```bash guide-step=46 on=edge-network
api='<value:OMNI_API_URL>'; api=${api#https://}
out=$(openssl s_client -connect "$api" -servername "${api%%:*}" -CAfile edge-ca.pem -verify_return_error </dev/null 2>&1 || true)
grep 'Verify return code: 0 (ok)' <<<"$out"
```

Success: `api` presents a certificate from your edge CA (`Verify return code: 0 (ok)`; it asks every
caller for a client certificate, which only edges and the relay have).

## 10. Sign in to the portal

Open the portal's address (`OMNI_WEB_URL`, recorded in `omnifile.state.env`) in a browser on your
users' network, and sign in as an ArchiveAdmin (in its group, or named in `OMNI_PEOPLE_ARCHIVEADMIN`):

```text guide-step=47 on=portal manual=yes
open /
expect Files
open /edges
expect Edges
```

Success: you are signed in, and the Edges page opens (empty for now). If the portal says you have no
role, check that your account is in `OMNI_GROUP_ARCHIVEADMIN` (or named in `OMNI_PEOPLE_ARCHIVEADMIN`,
and step 30 ran) and sign in again (Entra adds the role at sign-in).

## 11. Import the edge appliance

The edge filer is a virtual appliance: Ubuntu 26.04 LTS, minimized, with OmniFile's components on a
read-only, verified system partition that updates as a whole. Create its VM on **one** of the three
hypervisors below, with the sizes in section 1.6. Each VM needs its own copy of the appliance disk.

The system disk grows to fill whatever size you give it at the first start, so each recipe grows it
to 64 GiB first. The data disk stays empty: the setup wizard formats it.

### 11.1 Hyper-V (VHDX)

On the Hyper-V host, in PowerShell as an administrator, in the release folder. Set `$Switch` and
`$Dir` at the top first:

```powershell guide-step=48 on=hyperv manual=yes
$Release = '<value:OMNI_RELEASE>'
$Name    = '<value:OMNI_EDGE_HOSTNAME>'
$Switch  = 'LAN'                        # the virtual switch your users' network is on
$Dir     = 'D:\Hyper-V\omnifile-hou1'   # where this VM's disks go
$DataGB  = <value:OMNI_DATA_DISK_GB>

New-Item -ItemType Directory -Force -Path $Dir | Out-Null
Copy-Item ".\appliance-files\omnifile-edge_$Release.vhdx" "$Dir\system.vhdx"
Resize-VHD -Path "$Dir\system.vhdx" -SizeBytes 64GB
New-VHD -Path "$Dir\data.vhdx" -SizeBytes ($DataGB * 1GB) -Dynamic | Out-Null
New-VM -Name $Name -Generation 2 -MemoryStartupBytes 8GB -VHDPath "$Dir\system.vhdx" -SwitchName $Switch | Out-Null
Set-VMMemory -VMName $Name -DynamicMemoryEnabled $false
Set-VMProcessor -VMName $Name -Count 4
Set-VMFirmware -VMName $Name -EnableSecureBoot Off
Add-VMHardDiskDrive -VMName $Name -Path "$Dir\data.vhdx"
Set-VMKeyProtector -VMName $Name -NewLocalKeyProtector
Enable-VMTPM -VMName $Name
Set-VM -Name $Name -AutomaticCheckpointsEnabled $false
Disable-VMIntegrationService -VMName $Name -Name 'Time Synchronization'
Start-VM -Name $Name
vmconnect.exe localhost $Name
```

Success: the VM starts and its console window shows **OmniFile edge filer setup**. Time
synchronisation from the host is off because the filer takes its time from your domain controllers
(Kerberos needs the clocks within five minutes). Automatic checkpoints are off: a checkpoint
restored later would bring back an old filer that the service no longer agrees with.

### 11.2 vSphere (OVA)

With `govc` (VMware's command-line client) on a machine that reaches vCenter, in the release folder.
Set the `GOVC_` values at the top first. The OVA needs ESXi 7.0 U2 or later (virtual hardware 17).

```bash guide-step=49 on=vsphere manual=yes
export GOVC_URL='https://vcenter.corp.example' GOVC_USERNAME='administrator@vsphere.local'
export GOVC_DATACENTER='DC1' GOVC_DATASTORE='datastore1' GOVC_NETWORK='VM Network' GOVC_RESOURCE_POOL='*/Resources'
release='<value:OMNI_RELEASE>'
name='<value:OMNI_EDGE_HOSTNAME>'
data_gb='<value:OMNI_DATA_DISK_GB>'
read -rsp 'vCenter password: ' GOVC_PASSWORD && export GOVC_PASSWORD && echo

govc import.spec "appliance-files/omnifile-edge_$release.ova" |
  jq --arg net "$GOVC_NETWORK" '.NetworkMapping[0].Network = $net | .PowerOn = false' >ova-options.json
govc import.ova -options=ova-options.json -name="$name" "appliance-files/omnifile-edge_$release.ova"
govc vm.disk.change -vm "$name" -disk.label 'Hard disk 1' -size 64G
govc vm.disk.create -vm "$name" -name "$name/data" -size "${data_gb}G" -thick=false
govc vm.power -on "$name"
govc vm.console "$name"
```

Then add a virtual TPM in the vSphere Client: **Edit Settings → Add New Device → Trusted Platform
Module** (vCenter needs a key provider for this; without one, skip it and the filer seals its keys
with a key on its own disk). Do that before the first start if you can: power the VM off, add the
TPM, power it on.

Success: the VM's console (from `govc vm.console`, or the vSphere Client's web console) shows
**OmniFile edge filer setup**. In the vSphere Client the same import is **Deploy OVF Template**.

### 11.3 KVM (qcow2)

On a KVM host with libvirt and `virt-install`, as root, in a folder that holds a copy of the release
folder's `appliance-files/`. The filer joins the bridge named `OMNI_KVM_BRIDGE`:

```bash guide-step=50 on=kvm
release='<value:OMNI_RELEASE>'; name='<value:OMNI_EDGE_HOSTNAME>'; data_gb='<value:OMNI_DATA_DISK_GB>'; bridge='<value:OMNI_KVM_BRIDGE>'
dir=/var/lib/libvirt/images
cp "appliance-files/omnifile-edge_$release.qcow2" "$dir/$name.qcow2"
qemu-img resize "$dir/$name.qcow2" 64G
qemu-img create -f qcow2 "$dir/$name-data.qcow2" "${data_gb}G"
virt-install --name "$name" --memory 8192 --vcpus 4 --cpu host-passthrough \
  --boot uefi,firmware.feature0.name=secure-boot,firmware.feature0.enabled=no \
  --disk "path=$dir/$name.qcow2,format=qcow2,bus=virtio" \
  --disk "path=$dir/$name-data.qcow2,format=qcow2,bus=virtio" \
  --network "bridge=$bridge,model=virtio" \
  --tpm backend.type=emulator,backend.version=2.0,model=tpm-crb \
  --osinfo linux2022 --graphics vnc,listen=127.0.0.1 --import --noautoconsole
virsh vncdisplay "$name"
```

Open the VM's screen with `virt-viewer "$name"` (or a VNC viewer on the display printed). Setup asks
its questions on the VM's screen, not on the serial console. Success: the screen shows **OmniFile
edge filer setup**.

## 12. The setup wizard

The filer's console asks a few questions, in plain language, and checks each answer as you give it.
It shows a summary and asks once more before it changes anything. These are the answers for a filer
with one network adapter and one empty data disk, as above. Leave the enrolment code empty: section
13 gives it.

```text guide-step=51 on=edge-console
<value:OMNI_EDGE_HOSTNAME>
2
<value:OMNI_EDGE_ADDRESS>
<value:OMNI_EDGE_GATEWAY>
<value:OMNI_EDGE_DNS>
1
<value:OMNI_AD_DOMAIN>
<value:OMNI_AD_WORKGROUP>
<value:OMNI_AD_JOIN_USER>
<value:OMNI_AD_JOIN_OU>
<value:OMNI_AD_ADMIN_GROUP>
<secret:OMNI_AD_JOIN_PASSWORD>
y
<enter>
<enter>
<value:OMNI_EDGE_RESTART_WINDOW>
n
y
```

What each answer is:

| # | Question | Answer |
|---|---|---|
| 1 | What is this filer called on the network? | its name, at most 15 characters |
| 2 | How does it get its network address? | `2`, a fixed address (a file server shouldn't move) |
| 3–5 | Its address, the gateway, DNS servers | from `omnifile.env`. DNS: your domain controllers |
| 6 | Which disk holds the files? | `1`, the only empty disk. A disk that holds anything is formatted only after you type `ERASE`. |
| 7–10 | The AD domain, its short name, the account that may join computers, the OU | from `omnifile.env`. An empty OU means the domain's default |
| 11 | The AD group whose members administer this filer | `OMNI_AD_ADMIN_GROUP` |
| 12 | The join account's password | typed, not shown, never stored |
| 13 | Reach Azure storage only through private endpoints? | `y` (decision 22). The filer then refuses any storage name that resolves to a public address. |
| 14 | The enrolment code | Enter: you enrol in section 13 |
| 15 | The update feed's address | Enter keeps the one the appliance carries (OmniFile's) |
| 16 | When may it restart to apply an update? | your maintenance window. Updates download at any time; only the restart waits. |
| 17 | Will this filer serve files still in Amazon S3? | `n` (section 17 sets this up for migrations) |
| 18 | Set up the filer with these answers? | `y` after reading the summary |

It then works through its steps, one line each (`hostname done`, `network done`, …, `ad done: joined
<domain>`). The domain join takes up to a minute. `enrol` and `services` say `skipped`: the filer
enrols in the next section.

**The break-glass password.** During setup the console shows the password of `omnifile-recovery`,
the local account for when the domain can't be reached, **once**. Keep it where your break-glass
credentials live (a vault, or a sealed envelope). It logs in on the console only, never over ssh,
and every use of it is logged and shown in the portal until someone seals it again (`sudo
omnifile-setup recovery seal`).

Success: the console returns to a login prompt. From your workstation, an AD administrator of the
filer can now log in over ssh:

```bash guide-step=52 on=workstation manual=yes
. ./omnifile.env && . ./omnifile.state.env
ssh -t "$OMNI_AD_ADMIN_USER@$OMNI_EDGE_HOSTNAME.$OMNI_AD_DOMAIN" 'id; sudo omnifile-setup show'
```

It asks for the admin's AD password twice: to log in, and for sudo (`-t` gives sudo a terminal to ask
on). Success: `id` shows the AD user with its AD uid and groups, and `omnifile-setup show` lists the
answers you gave. A refusal at login means the user has no `loginShell` or isn't in the admin group
(section 1.4).

To change an answer later, run `sudo omnifile-setup` again on the console: your previous answers are
the defaults, it keeps the data disk it formatted (it asks you to confirm), and only what you change is
redone.

## 13. Enrol the edge from the portal

The filer joins the service with a one-time code from the portal. In the portal, as an
ArchiveAdmin:

```text guide-step=53 on=portal
open /edges
click Add edge
click Skip to the code
fill Name = <value:OMNI_EDGE_NAME>
click Create code
expect Copy this code now
click Copy code
```

Add edge opens a five-step wizard: the platform, its image, the VM's size and network, the code,
and the first heartbeat. You did the first three in sections 1.6, 11 and 12, so **Skip to the code**
goes straight to the code's step. The code starts with `ofe1.` and is shown only once. It works once,
until the time shown (24 hours).

On the filer, give it the code. It asks for it; paste it and press Enter:

```bash guide-step=54 on=edge-shell input=OMNI_ENROL_CODE
set -euo pipefail
sudo omnifile-setup enrol
```

It enrols (the filer makes its key, sealed to the TPM, and gets its certificate from the edge CA),
then starts the file servers. Success: `enrol done` and `services done`. Check them:

```bash guide-step=55 on=edge-shell
set -euo pipefail
systemctl is-active omnifile-edge smbd nfs-ganesha-omni
log=$(sudo journalctl -u omnifile-edge --since '-10 min' --output cat)
grep -E 'startup checks passed' <<<"$log"
```

Success: three `active` lines, and `startup checks passed` in the log. In the portal, the edge is
listed with a recent heartbeat:

```text guide-step=56 on=portal wait=5
open /edges
expect <value:OMNI_EDGE_NAME>
expect Last heartbeat
```

If the code was lost or has expired, press **New code** next to the pending edge and run step 54
again with the new one.

## 14. The first dataset and share

A share is a folder on the filer that people reach over SMB, NFS or both. Its data lives in a
dataset: here, a new one in the Azure storage you registered in step 26. In the portal, as an
ArchiveAdmin:

```text guide-step=57 on=portal
open /shares
click Add share
fill Share name = <value:OMNI_SHARE>
click In a new dataset
choose Which edge serves it? = <value:OMNI_EDGE_NAME>
choose Where is it stored? = azure
click Windows (SMB)
click Linux/Unix (NFSv4)
click Trust named hosts (sec=sys)
fill Trusted hosts and subnets = <value:OMNI_NFS_CLIENTS>
fill Owners group = <value:OMNI_SHARE_OWNERS>
click <value:OMNI_SHARE_OWNERS>
fill Writers group = <value:OMNI_SHARE_WRITERS>
click <value:OMNI_SHARE_WRITERS>
fill Readers group = <value:OMNI_SHARE_READERS>
click <value:OMNI_SHARE_READERS>
click Tell them straight away
click Create share
expect is ready
```

What the choices mean:

- **Who connects.** Windows (SMB) and Linux (NFSv4). NFSv3 is there for older clients and some
  clusters. NFSv3 clients mount as usual, with locking: the filer runs NFSv3's lock service (NLM), so
  locks hold across clients. The client needs `rpc.statd` (Ubuntu and Debian: `nfs-common`; RHEL:
  `nfs-utils`) running.
- **How NFS clients prove who they are.** This guide trusts named hosts (`sec=sys`): the hosts you
  list say which user is asking, and root on them is mapped to nobody. **Kerberos** proves each user
  instead, but costs throughput: Microsoft measured about **53% fewer IOPS with `krb5`** and about
  **77% fewer with `krb5p`** (which also encrypts). The portal shows the same note when you choose it.
  Kerberos needs each Linux client joined to AD with a keytab; you can switch a share to it later in
  its settings.
- **Who has access.** Owners can change the share's settings and the other groups; Writers read and
  write; Readers read. The filer enforces these with the folder's ACL on every path: SMB, NFS, the
  portal and Teams.
- **When someone opens an archived file.** "Tell them straight away" suits people: the open fails at
  once, a note `<file>.RECALLING.txt` appears next to it, and they get a Teams card. "Make the program
  wait" suits batch clusters: the read waits until the file is back. "Wait only if it's quick" waits
  up to a limit you set, then fails fast.

Every other setting (SMB encryption, how far back people may rewind, the storage layout) is under
**More settings**, and can be changed later.

One setting is on the share's own page once it exists: **how long a file must sit untouched before
OmniFile treats it as finished**. The default is 10 seconds, or 60 on a WORM share. NFSv3 has no open
or close, so a copy that pauses for longer than twice this time leaves a partial version, then the
finished file as the next one. On a WORM share that partial version can't be removed until retention
ends. Longer means fewer partial versions, but uploads start later. You can set 10 to 600 seconds.

Success: **projects is ready** (with your share's name) and the paths to use: `\\<filer>\<share>`
and `<filer>:/<share>`. Within a minute the filer serves it:

```bash guide-step=58 on=edge-shell wait=5
set -euo pipefail
log=$(sudo journalctl -u omnifile-edge --since '-10 min' --output cat)
grep 'share config changed' <<<"$log"
sudo grep -c '^\[' /var/lib/omnifile/shares/smb.conf
sudo grep -c 'Export_Id' /var/lib/omnifile/shares/ganesha.conf
```

Success: `share config changed: reloading the file servers`, and a count of at least 1 for both
servers.

## 15. Check that it works

These checks use the share as people will. The Linux client must be on the users' network
(`OMNI_USER_CIDRS`) and in `OMNI_NFS_CLIENTS`, use the
filer's DNS name, and have the `nfs-common`, `smbclient` and `krb5-user` packages (Ubuntu or Debian
names). Its own copy of the values it needs comes first.

### 15.1 Write over NFS and SMB

Mount the share over NFSv4 and write a test file as the test user (a member of Writers). With
`sec=sys` the filer takes the uid the client sends, so the client acts as that uid:

```bash guide-step=59 on=linux-client
filer='<value:OMNI_EDGE_HOSTNAME>.<value:OMNI_AD_DOMAIN>'; share='<value:OMNI_SHARE>'
mnt='<value:OMNI_CLIENT_MOUNT>'; uid='<value:OMNI_TEST_UID>'; gid='<value:OMNI_DOMAIN_USERS_GID>'
sudo mkdir -p "$mnt"
mountpoint -q "$mnt" || sudo mount -t nfs4 -o vers=4.1,hard "$filer:/$share" "$mnt"
sudo setpriv --reuid "$uid" --regid "$gid" --clear-groups -- \
  sh -c "head -c 256M /dev/urandom >'$mnt/guide-nfs.bin' && sha256sum '$mnt/guide-nfs.bin'" | tee guide-nfs.sha256
ls -ln "$mnt/guide-nfs.bin"
```

Success: the file is written, owned by the test user's uid, and its SHA-256 is saved in
`guide-nfs.sha256`. (`--regid` sets the user's primary group, Domain Users; the filer looks up the user's AD
groups itself.)

Then over SMB, as the same AD user, with Kerberos. `kinit` asks for the user's password:

```bash guide-step=60 on=linux-client input=OMNI_TEST_USER_PASSWORD
filer='<value:OMNI_EDGE_HOSTNAME>.<value:OMNI_AD_DOMAIN>'; share='<value:OMNI_SHARE>'; user='<value:OMNI_TEST_USER>'
realm=$(tr '[:lower:]' '[:upper:]' <<<'<value:OMNI_AD_DOMAIN>')
kinit "$user@$realm"
head -c 64M /dev/urandom >guide-smb.bin && sha256sum guide-smb.bin | tee guide-smb.sha256
smbclient "//$filer/$share" --use-kerberos=required -c 'put guide-smb.bin guide-smb.bin; allinfo guide-smb.bin'
```

Success: `putting file guide-smb.bin`, then its details. The filer saw the AD user: the file is
theirs over NFS too (`ls -l /mnt/omnifile/guide-smb.bin` on the NFS mount shows their name or uid).

### 15.2 Someone outside the share's groups is refused

```bash guide-step=61 on=linux-client input=OMNI_OTHER_USER_PASSWORD
filer='<value:OMNI_EDGE_HOSTNAME>.<value:OMNI_AD_DOMAIN>'; share='<value:OMNI_SHARE>'; other='<value:OMNI_OTHER_USER>'
realm=$(tr '[:lower:]' '[:upper:]' <<<'<value:OMNI_AD_DOMAIN>')
kinit "$other@$realm"
if smbclient "//$filer/$share" --use-kerberos=required -c 'ls' 2>&1 | tee guide-refused.txt; then
  grep -qE 'NT_STATUS_(ACCESS_DENIED|BAD_NETWORK_NAME)' guide-refused.txt || { echo "NOT refused"; exit 1; }
fi
kdestroy
grep -E 'NT_STATUS_(ACCESS_DENIED|BAD_NETWORK_NAME)' guide-refused.txt
```

Success: `NT_STATUS_ACCESS_DENIED` (or `NT_STATUS_BAD_NETWORK_NAME`: the share is hidden from people
who can't use it).

### 15.3 The files reach Azure

The filer uploads a file once it has sat untouched for about twice the share's quiet period, so
about 20 seconds here. In the portal:

```text guide-step=62 on=portal wait=10
open /files
click <value:OMNI_SHARE>
expect guide-nfs.bin
expect guide-smb.bin
```

Success: both files are listed, with their size and their tier. They are plain blobs in your live
storage account, readable by any Azure tool.

### 15.4 The local copy becomes a stub, and reads back

About an hour after a file is written (with no tiering policy yet), the filer frees its local copy
and keeps only a stub: the name, the size, and the first few megabytes. Wait an hour, then on the
Linux client:

```bash guide-step=63 on=linux-client wait=90
mnt='<value:OMNI_CLIENT_MOUNT>'
f="$mnt/guide-nfs.bin"
size=$(stat -c %s "$f")
used=$(( $(stat -c %b "$f") * $(stat -c %B "$f") ))
echo "guide-nfs.bin: $size bytes, $used of them on the filer's disk"
[ "$used" -lt $((size / 2)) ] || { echo "not a stub yet: run this step again later"; exit 1; }
sha256sum --check guide-nfs.sha256
```

Success: only a few MB of the 256 MB file are on the filer's disk (the stub), and the checksum is
`OK`: reading the file brought its bytes back from Azure without anyone noticing. Until the stub is
made, the step says `not a stub yet`: run it again later.

### 15.5 Archive and recall

Put the NFS test file in Archive by hand, with a pin (decision 29). In the portal:

```text guide-step=64 on=portal
open /files
click <value:OMNI_SHARE>
click guide-nfs.bin
click Pin to a tier
click Archive
click Until someone releases it
fill Why? = Customer guide check
click I've read the warnings above
click Apply pin
expect Pinned on this item
```

The portal lists what the pin costs (Archive bills at least 180 days) and what it means (reads need
a recall), which you acknowledge. The service then moves the file to the archive account; the
portal shows its tier as Archive once the move is done (allow up to an hour):

```text guide-step=65 on=portal wait=90
open /files
click <value:OMNI_SHARE>
expect Archive
```

Now bring it back, as someone would before they need it, at High priority. How long a recall takes
depends on Azure: up to 15 hours at Standard priority for a file under 10 GB, and usually under an
hour at High priority for a file under 10 GB. OmniFile promises no time for files above 10 GB, and
the portal says so for them.

```text guide-step=66 on=portal
open /files
click <value:OMNI_SHARE>
click guide-nfs.bin
click Recall ahead of time
click High priority
click Recall file
expect Recall progress
```

Success: the recall's page, with its four steps (requested, rehydrating, downloading, available).
While it runs, open the file on the share, which has the fail-fast behaviour:

```bash guide-step=67 on=linux-client
mnt='<value:OMNI_CLIENT_MOUNT>'
if cat "$mnt/guide-nfs.bin" >/dev/null 2>guide-open.txt; then echo "the open did not fail: is the recall already done?"; exit 1; fi
cat guide-open.txt
sleep 5
cat "$mnt/guide-nfs.bin.RECALLING.txt"
```

Success: `Input/output error` at once (never "No such file": the file is there, just not online),
and the note beside it says the file is being recalled and where to follow it. The person who opened
it (here, the test user) gets a Teams card with **Recall**, **Keep online** and **Track**, which
updates as the recall goes on. (With `sec=sys`, OmniFile knows the user only by the uid the client
sent, and records it as claimed.)

Once the recall is done (the card and the portal say the file is available), read it again:

```bash guide-step=68 on=linux-client wait=240
sha256sum --check guide-nfs.sha256
```

Success: `OK`, the same bytes that were written.

### 15.6 Versions

Every committed write is a version. Overwrite the SMB test file and look at its versions:

```bash guide-step=69 on=linux-client
filer='<value:OMNI_EDGE_HOSTNAME>.<value:OMNI_AD_DOMAIN>'; share='<value:OMNI_SHARE>'
head -c 1M /dev/urandom >guide-smb.bin
smbclient "//$filer/$share" --use-kerberos=required -c 'put guide-smb.bin guide-smb.bin'
kdestroy
```

```text guide-step=70 on=portal
open /files
click <value:OMNI_SHARE>
click guide-smb.bin
expect Versions
```

Success: the file's panel lists two versions. Restoring the older one (as `guide-smb-restored.bin`,
or over the current one, which is kept as a version) is in the same panel.

You now have a working deployment: the service, an enrolled filer, a share over SMB and NFS with AD
access control, uploads, stubs, Archive, recalls and Teams cards. Next, set a tiering policy for the
dataset (**Policies** in the portal): when files go to warm, cold and archive, or Smart plus an
archive date.

## 16. Troubleshooting

Start with the logs. On the filer:

```bash
sudo journalctl -u omnifile-edge -u smbd -u nfs-ganesha-omni --since '-1 hour'
sudo omnifile-setup show
```

In Azure, for one app (`api`, `enrol`, `web`, `worker`, `relay`):

```bash
az containerapp logs show --resource-group "$OMNI_RG" --name "$OMNI_PREFIX-worker" --tail 100
```

| What you see | Why | What to do |
|---|---|---|
| Step 14 fails: `relay-allowed-cidrs.json` not found | Step 13 hasn't run in this release folder | Run step 13, then step 14 again. |
| Step 15 refuses: the preview changed | Something changed since step 14 (a value, a new relay range) | Run step 14 again, read the new preview, then step 15. |
| An app is still not ready after phase 2 | Its log says what it waits for: often `OMNI_WEB_CLIENT_ID` wasn't recorded before step 41, or a secret is missing | Run the step that records it (step 28 for the portal; step 39 for the secrets, after giving yourself the vault role again, since step 43 removed it: `az role assignment create --assignee "$OMNI_ADMIN_OBJECT_ID" --role "Key Vault Secrets Officer" --scope <the vault's id>`), then steps 41 and 42, then step 43. |
| `api` or `enrol` answer `503 unavailable` to everything but `/healthz` | They wait for the edge CA certificate | Do section 8.3, then phase 2. |
| Step 23: a name resolves to a public address | A private DNS zone isn't linked to the hub, or a forwarder is missing | Check steps 18 and 22. The filer refuses public addresses for storage (private endpoints only). |
| psql: `password authentication failed` | The token expired (an hour), or you aren't in `OMNI_PG_ADMIN_GROUP` | Run the step again (it takes a fresh token); check your membership. |
| Step 23: the database's name doesn't resolve, or resolves to a public address | Its private zone isn't linked to the hub, or there is no `postgres.database.azure.com` forwarder | Check steps 18 and 22: `nslookup <server>.postgres.database.azure.com` from the workstation must give an address in `OMNI_SUBNET_PG`. |
| psql: no route to the server | The workstation isn't in `OMNI_ADMIN_CIDRS`, or peering isn't connected | Check section 6.3 and the value; redeploy if you changed it. |
| Step 39: `Forbidden` from Key Vault | The role assignment is still propagating | Wait five minutes and run it again. |
| Add edge says enrolment isn't configured | `enrolServerCaSha256` wasn't set | Run step 38, then steps 41 and 42. |
| The wizard stops at the domain join | DNS doesn't find the domain, the clock is off by more than 5 minutes, the password is wrong, or the account can't create computers in that OU | Fix it; the console asks the questions again by itself, with your answers as the defaults. It offers to keep the data disk it already formatted: answer `y`, and nothing is formatted again. |
| The wizard says the admin group has no `gidNumber` | Section 1.4 | Give the group a `gidNumber` (step 3), then run the wizard again. |
| An admin can't log in over ssh | No `loginShell` in AD, or not in the admin group | Step 3 with `-LoginShellGroup`; check the group. |
| A group isn't offered when adding a share | The portal's group list refreshes once a day | Wait, or restart the worker: `az containerapp revision restart` on its active revision. |
| A share isn't served (step 58 shows nothing) | Its Owners group has no `gidNumber`, or winbind can't see it yet | `wbinfo --group-info=<group>` on the filer; fix the attribute; the filer retries every 30 seconds. |
| An API client gets `403 client_not_registered` | Its application isn't registered in Settings, API clients, or is disabled or revoked there | Register it, or enable it (section 18). The application (client) id must be the one its token carries. |
| An API client's proposal gets `403 owner_lacks_access` | The client's owner can't see the dataset | The owner signs in to the portal as an ArchiveAdmin or DataSteward (that counts for 30 days), or is made an Owner of one of the dataset's shares; or change the client's owner (section 18). |
| NFSv3 locks fail or hang (`No locks available`) | The client's `rpc.statd` isn't running, or a firewall between client and filer blocks the lock protocol | On the client, install `nfs-common` (or `nfs-utils`) and check that `rpcinfo -p <filer>` lists `nlockmgr` on 32803 and `status` on 32765. To the filer, allow TCP and UDP 111 (rpcbind), 20048 (MOUNT), 32803 (NLM) and 32765 (statd), and TCP 2049 (NFS); the filer accepts nothing else but 22 and 445. To the client, allow its own lockd and statd ports (set in its `nfs.conf`): the filer calls back on them, and its statd notifies clients after a restart from port 32766. |
| Opening an archived file gives `Input/output error` | That is the fail-fast behaviour, by design | A recall has started; see the `.RECALLING.txt` note and the Teams card. |
| No Teams card arrives | The person isn't synced to Entra with their AD SID, the Teams app isn't allowed (step 31), or the worker lacks its Graph permissions (step 28) | Check the worker's log for the identity lookup. Email is the fallback. |
| The portal shows the filer needing attention: recovery used | Someone used the break-glass account | Find out why, then `sudo omnifile-setup recovery seal` on the console. |
| The relay's certificate is about to expire (180 days) | It is renewed by hand | Give yourself the upload right again for the renewal (`az role assignment create --assignee "$OMNI_ADMIN_OBJECT_ID" --role "Key Vault Secrets Officer" --scope <the vault's id>`), run steps 34 to 36, then steps 39 to 42, then step 43 to remove the right again. |

## 17. Amazon S3: for migration clients

Only if an API client is migrating data to you from Amazon S3 and the share should show that data while
it moves. OmniFile reads the S3 data with **no keys anywhere** (decision 43): the service signs in to
AWS with its Azure managed identity, and each filer with IAM Roles Anywhere, using a certificate from
your edge CA (decision 50) and a key sealed in its TPM. OmniFile never writes to or deletes from a
source bucket (decision 40).

You need: an AWS account with the source buckets, a VPC that your data centre and the service's
Azure network both reach (Direct Connect or VPN), the AWS CLI v2 signed in to that account on the
workstation, and the S3 values in `omnifile.env`. Every AWS call below is made by you; `deploy/aws/README.md`
explains each piece. The names below are the ones you get: only a test run, with
`OMNI_AWS_NAME_SUFFIX` set, adds `-<suffix>` to each.

### 17.1 Interface endpoints

OmniFile reaches AWS only through interface endpoints: S3 (data and S3 Batch Operations), STS (the
service's sign-in), SQS (S3 events) and IAM Roles Anywhere (the filers' sign-in, and the CRL upload):

```bash guide-step=71 on=workstation
. ./omnifile.env && . ./omnifile.state.env
[ -n "$OMNI_AWS_ACCOUNT_ID" ] || { echo "no S3 values in omnifile.env: section 17 skipped"; exit 0; }
sfx=${OMNI_AWS_NAME_SUFFIX:+-$OMNI_AWS_NAME_SUFFIX}
subnets=$(tr ',' ' ' <<<"$OMNI_AWS_SUBNET_IDS")
for svc in s3 sts sqs rolesanywhere; do
  id=$(aws ec2 describe-vpc-endpoints --region "$OMNI_AWS_REGION" \
    --filters "Name=vpc-id,Values=$OMNI_AWS_VPC_ID" "Name=service-name,Values=com.amazonaws.$OMNI_AWS_REGION.$svc" \
    "Name=vpc-endpoint-type,Values=Interface" --query 'VpcEndpoints[0].VpcEndpointId' --output text)
  if [ "$id" = None ]; then
    # shellcheck disable=SC2086
    id=$(aws ec2 create-vpc-endpoint --region "$OMNI_AWS_REGION" --vpc-id "$OMNI_AWS_VPC_ID" \
      --vpc-endpoint-type Interface --service-name "com.amazonaws.$OMNI_AWS_REGION.$svc" \
      --subnet-ids $subnets --security-group-ids "$OMNI_AWS_SG_ID" --no-private-dns-enabled \
      --tag-specifications "ResourceType=vpc-endpoint,Tags=[{Key=Name,Value=omnifile-$svc$sfx}]" \
      --query VpcEndpoint.VpcEndpointId --output text)
  fi
  dns=$(aws ec2 describe-vpc-endpoints --region "$OMNI_AWS_REGION" --vpc-endpoint-ids "$id" \
    --query 'VpcEndpoints[0].DnsEntries[0].DnsName' --output text)
  name=$(tr '[:lower:]' '[:upper:]' <<<"$svc")
  docs/guide/tools/state.sh set "OMNI_AWS_VPCE_$name" "$id"
  docs/guide/tools/state.sh set "OMNI_AWS_VPCE_${name}_DNS" "${dns#\*.}"
done
```

The security group must allow HTTPS (443) in from the filers' networks and from the service's Azure
range (`OMNI_VNET_PREFIX`). The endpoints' own DNS names resolve, from anywhere, to their private
addresses, so no DNS changes are needed. Success: eight `recorded` lines.

### 17.2 Roles and trust

The service's role, which its managed identities assume with an Entra token (web identity), the role
S3 Batch Operations runs restores and checksums as, and the filers' role. `docs/guide/aws/render.sh`
fills your values into the policy templates in `docs/guide/aws/` and `deploy/aws/iam/`:

```bash guide-step=72 on=workstation
. ./omnifile.env && . ./omnifile.state.env
[ -n "$OMNI_AWS_ACCOUNT_ID" ] || { echo "no S3 values in omnifile.env: section 17 skipped"; exit 0; }
sfx=${OMNI_AWS_NAME_SUFFIX:+-$OMNI_AWS_NAME_SUFFIX}
docs/guide/tools/state.sh set OMNI_AWS_AUDIENCE "api://$OMNI_WEB_CLIENT_ID"
. ./omnifile.state.env
have=$(aws iam list-open-id-connect-providers \
  --query "OpenIDConnectProviderList[?contains(Arn, 'sts.windows.net/$OMNI_TENANT_ID')].Arn | [0]" --output text)
[ "$have" != None ] ||
  aws iam create-open-id-connect-provider --url "https://sts.windows.net/$OMNI_TENANT_ID/" \
    --client-id-list "$OMNI_AWS_AUDIENCE" >/dev/null
docs/guide/aws/render.sh --out aws-policies
for r in omnifile-service$sfx:service-role omnifile-batch$sfx:batch-role; do
  role=${r%%:*} file=${r#*:}
  aws iam get-role --role-name "$role" >/dev/null 2>&1 ||
    aws iam create-role --role-name "$role" --assume-role-policy-document "file://aws-policies/$file-trust.json" >/dev/null
  aws iam update-assume-role-policy --role-name "$role" --policy-document "file://aws-policies/$file-trust.json"
  aws iam put-role-policy --role-name "$role" --policy-name omnifile --policy-document "file://aws-policies/$file-policy.json"
  echo "role $role ready"
done
```

Success: `role omnifile-service ready` and `role omnifile-batch ready`. Only the service's `api` and
`worker` identities may assume the service role, and only with a token for `OMNI_AWS_AUDIENCE`. The
service may hand a job the batch role and no other, and only S3 Batch Operations, for jobs of this
account, may assume that role.

The trust anchor is your edge CA certificate (step 33), and the profile hands out sessions of at
most an hour. The filers' role admits only certificates with `OU=omnifile-roles-anywhere` and the
names of the filers enrolled so far, which this reads from the catalog:

```bash guide-step=73 on=workstation
. ./omnifile.env && . ./omnifile.state.env
[ -n "$OMNI_AWS_ACCOUNT_ID" ] || { echo "no S3 values in omnifile.env: section 17 skipped"; exit 0; }
sfx=${OMNI_AWS_NAME_SUFFIX:+-$OMNI_AWS_NAME_SUFFIX}
ta=$(aws rolesanywhere list-trust-anchors --region "$OMNI_AWS_REGION" \
  --query "trustAnchors[?name=='omnifile-edge-ca$sfx'].trustAnchorArn | [0]" --output text)
if [ "$ta" = None ]; then
  ta=$(aws rolesanywhere create-trust-anchor --region "$OMNI_AWS_REGION" --name "omnifile-edge-ca$sfx" --enabled \
    --source "sourceType=CERTIFICATE_BUNDLE,sourceData={x509CertificateData=$(cat edge-ca.pem)}" \
    --query trustAnchor.trustAnchorArn --output text)
fi
docs/guide/tools/state.sh set OMNI_AWS_TRUST_ANCHOR_ARN "$ta"
. ./omnifile.state.env
edges=$(PGPASSWORD=$(az account get-access-token --resource-type oss-rdbms --query accessToken --output tsv) \
  psql "host=$OMNI_PG_HOST dbname=$OMNI_PG_DATABASE user='$OMNI_PG_ADMIN_GROUP' sslmode=verify-full sslrootcert=system" \
  -At -c "SELECT string_agg(edge_id::text, ',' ORDER BY edge_id) FROM catalog.edge WHERE state = 'active'")
docs/guide/aws/render.sh --out aws-policies --edge-ids "$edges"
aws iam put-role-policy --role-name "omnifile-service$sfx" --policy-name omnifile-crl \
  --policy-document file://aws-policies/service-crl-policy.json
aws iam get-role --role-name "omnifile-edge-s3-read$sfx" >/dev/null 2>&1 ||
  aws iam create-role --role-name "omnifile-edge-s3-read$sfx" \
    --assume-role-policy-document file://aws-policies/edge-role-trust.json >/dev/null
aws iam update-assume-role-policy --role-name "omnifile-edge-s3-read$sfx" --policy-document file://aws-policies/edge-role-trust.json
aws iam put-role-policy --role-name "omnifile-edge-s3-read$sfx" --policy-name omnifile \
  --policy-document file://aws-policies/edge-role-policy.json
profile=$(aws rolesanywhere list-profiles --region "$OMNI_AWS_REGION" \
  --query "profiles[?name=='omnifile-edges$sfx'].profileArn | [0]" --output text)
if [ "$profile" = None ]; then
  profile=$(aws rolesanywhere create-profile --region "$OMNI_AWS_REGION" --name "omnifile-edges$sfx" --enabled \
    --role-arns "arn:aws:iam::$OMNI_AWS_ACCOUNT_ID:role/omnifile-edge-s3-read$sfx" --duration-seconds 3600 \
    --query profile.profileArn --output text)
fi
docs/guide/tools/state.sh set OMNI_AWS_PROFILE_ARN "$profile"
```

Success: the trust anchor and the profile are recorded. Run this step again after you enrol
another filer, so its name is admitted too. The service uploads the edge CA's revocation list to
the trust anchor whenever you revoke a filer in the portal (the `omnifile-crl` policy allows that and
nothing else).

### 17.3 S3 events

The service learns of restores and changes from EventBridge, through one SQS queue. Each source bucket
must send its events to EventBridge. A bucket's notification settings are one document that a write
replaces whole, so this step reads each bucket's settings, adds EventBridge to them, and writes them
back: notifications the bucket already sends elsewhere stay as they are.

```bash guide-step=74 on=workstation
. ./omnifile.env && . ./omnifile.state.env
[ -n "$OMNI_AWS_ACCOUNT_ID" ] || { echo "no S3 values in omnifile.env: section 17 skipped"; exit 0; }
sfx=${OMNI_AWS_NAME_SUFFIX:+-$OMNI_AWS_NAME_SUFFIX}
q=$(aws sqs create-queue --region "$OMNI_AWS_REGION" --queue-name "omnifile-events$sfx" --query QueueUrl --output text)
qarn=$(aws sqs get-queue-attributes --region "$OMNI_AWS_REGION" --queue-url "$q" --attribute-names QueueArn \
  --query Attributes.QueueArn --output text)
rule=$(aws events put-rule --region "$OMNI_AWS_REGION" --name "omnifile-s3$sfx" \
  --event-pattern "$(jq -nc --arg b "$OMNI_AWS_SOURCE_BUCKETS" '{source: ["aws.s3"],
     "detail-type": ["Object Created", "Object Deleted", "Object Restore Initiated", "Object Restore Completed",
                     "Object Restore Expired", "Object Storage Class Changed", "Object Access Tier Changed"],
     detail: {bucket: {name: ($b | split(","))}}}')" --query RuleArn --output text)
aws sqs set-queue-attributes --region "$OMNI_AWS_REGION" --queue-url "$q" --attributes "$(jq -nc --arg q "$qarn" --arg r "$rule" \
  '{Policy: ({Version: "2012-10-17", Statement: [{Effect: "Allow", Principal: {Service: "events.amazonaws.com"},
     Action: "sqs:SendMessage", Resource: $q, Condition: {ArnEquals: {"aws:SourceArn": $r}}}]} | tojson)}')"
aws events put-targets --region "$OMNI_AWS_REGION" --rule "omnifile-s3$sfx" --targets "Id=omnifile-sqs,Arn=$qarn" >/dev/null
for b in $(tr ',' ' ' <<<"$OMNI_AWS_SOURCE_BUCKETS"); do
  now=$(aws s3api get-bucket-notification-configuration --bucket "$b" --output json)
  [ -n "$now" ] || now='{}'
  jq 'del(.ResponseMetadata) + {EventBridgeConfiguration: {}}' <<<"$now" >"notify-$b.json"
  aws s3api put-bucket-notification-configuration --bucket "$b" --notification-configuration "file://notify-$b.json"
  echo "bucket $b sends its events to EventBridge, and keeps its other notifications"
done
docs/guide/tools/state.sh set OMNI_AWS_EVENT_QUEUE_URL "$q"
```

Success: one line per bucket, and the queue recorded. `notify-<bucket>.json` holds what each bucket's
settings are now.

AWS takes about five minutes to start sending a bucket's events after this step. A change made in
a bucket during those minutes sends no event, so wait five minutes before you go on.

### 17.4 Tell the service

Compose the service's AWS settings, register S3 as a migration source in the catalog, and deploy
again:

```bash guide-step=75 on=workstation
. ./omnifile.env && . ./omnifile.state.env
[ -n "$OMNI_AWS_ACCOUNT_ID" ] || { echo "no S3 values in omnifile.env: section 17 skipped"; exit 0; }
docs/guide/aws/accounts-json.sh >aws-accounts.json
jq . aws-accounts.json
docs/guide/tools/state.sh set-file OMNI_AWS_ACCOUNTS_JSON aws-accounts.json
PGPASSWORD=$(az account get-access-token --resource-type oss-rdbms --query accessToken --output tsv) \
OMNIFILE_DATABASE_URL="host=$OMNI_PG_HOST dbname=$OMNI_PG_DATABASE user='$OMNI_PG_ADMIN_GROUP' sslmode=verify-full sslrootcert=system" \
OMNIFILE_AWS_ACCOUNTS_FILE=aws-accounts.json OMNIFILE_AWS_WEB_IDENTITY_AUDIENCE="$OMNI_AWS_AUDIENCE" \
  uv run --project service --no-dev python -m omnifile_service.admin backend add s3 \
  --account "$OMNI_AWS_ACCOUNT_ID" --region "$OMNI_AWS_REGION" --buckets "$OMNI_AWS_SOURCE_BUCKETS"
```

Success: `added: s3-<account> (backend N, s3, private network): account <account>, <region>, buckets
<buckets>` (or `unchanged: ...`). The row is checked against `aws-accounts.json` as the worker will
check it, so a bucket that is `OMNI_AWS_OPS_BUCKET`, or a region other than the account's, is refused
before anything is written. The next two steps redeploy `api` and the worker, which read it.

```bash guide-step=76 on=workstation gate=whatif
. ./omnifile.env && . ./omnifile.state.env
[ -n "$OMNI_AWS_ACCOUNT_ID" ] || { echo "no S3 values in omnifile.env: section 17 skipped"; exit 0; }
deploy/azure/relay-ranges.sh --location "$OMNI_LOCATION" --out deploy/azure/relay-allowed-cidrs.json
deploy/azure/whatif.sh -g "$OMNI_RG" -p docs/guide/customer.bicepparam | tee whatif-aws.txt
docs/guide/tools/state.sh set OMNI_WHATIF_DIGEST "$(tail -n 1 whatif-aws.txt | sed 's/^digest: //')"
```

Read `whatif-aws.txt`: only `api` and `worker` change (their AWS settings). Then:

```bash guide-step=77 on=workstation
. ./omnifile.env && . ./omnifile.state.env
[ -n "$OMNI_AWS_ACCOUNT_ID" ] || { echo "no S3 values in omnifile.env: section 17 skipped"; exit 0; }
deploy/azure/whatif.sh -g "$OMNI_RG" -p docs/guide/customer.bicepparam --deploy --approve "$OMNI_WHATIF_DIGEST"
```

Success: `Succeeded`. The worker's log shows no AWS settings error.

### 17.5 The filer

Give the filer its Roles Anywhere settings. This writes them as a section of the filer's setup
answers:

```bash guide-step=78 on=workstation
. ./omnifile.env && . ./omnifile.state.env
[ -n "$OMNI_AWS_ACCOUNT_ID" ] || { echo "no S3 values in omnifile.env: section 17 skipped"; exit 0; }
sfx=${OMNI_AWS_NAME_SUFFIX:+-$OMNI_AWS_NAME_SUFFIX}
role="arn:aws:iam::$OMNI_AWS_ACCOUNT_ID:role/omnifile-edge-s3-read$sfx"
cat >edge-aws.toml <<EOF

[aws]
trust_anchor_arn = "$OMNI_AWS_TRUST_ANCHOR_ARN"
profile_arn = "$OMNI_AWS_PROFILE_ARN"
role_arn = "$role"
endpoint = "https://$OMNI_AWS_VPCE_ROLESANYWHERE_DNS"
EOF
scp edge-aws.toml "$OMNI_AD_ADMIN_USER@$OMNI_EDGE_HOSTNAME.$OMNI_AD_DOMAIN:edge-aws.toml"
```

On the filer, put the section in the setup answers (replacing an `[aws]` section already there, so a
rerun gives the same file; the new file is checked before it replaces the old one) and run only the
`aws` step. The filer makes a new key, sealed to the TPM,
and asks the service for its certificate (from your edge CA, renewed by itself):

```bash guide-step=79 on=edge-shell
set -euo pipefail
[ -f edge-aws.toml ] || { echo "no edge-aws.toml: section 17 skipped"; exit 0; }
sudo install -m 0600 /etc/omnifile/setup.toml /etc/omnifile/setup.toml.before-aws
new=$(sudo mktemp /etc/omnifile/setup.toml.XXXXXX)
trap 'sudo rm -f "$new"' EXIT
sudo awk '/^\[aws\]/ { skip = 1; next } /^\[/ { skip = 0 } !skip { line[++n] = $0 }
  END { while (n > 0 && line[n] == "") n--; for (i = 1; i <= n; i++) print line[i] }' /etc/omnifile/setup.toml.before-aws |
  cat - edge-aws.toml | sudo tee "$new" >/dev/null
[ "$(sudo grep -c '^\[aws\]' "$new")" = 1 ] && sudo grep -q '^version' "$new" ||
  { echo "the new setup.toml isn't right: left as it was"; exit 1; }
sudo chmod 0600 "$new"
sudo mv "$new" /etc/omnifile/setup.toml
sudo omnifile-setup check
sudo omnifile-setup apply --only aws
systemctl is-active omnifile-aws-signing-helper
```

Success: `aws done`, and the signing helper `active`. The API client running the migration then
registers its sources through the API; their files appear in the share as they are found, and are read
from S3 until each one has moved. Register that client first (section 18).

## 18. API clients

The API accepts a token only from an application an ArchiveAdmin has registered and enabled in
**Settings, API clients**; every other application is refused (`403 client_not_registered`). Do this for
each API client, with its values in the "API clients" part of `omnifile.env`: its application (client)
id in Entra, a name, its scopes, and its owner. Without one, skip this section.

The scopes are the API's application roles, and a client may use only the ones it holds in Entra too:

| Scope | Lets the client |
|---|---|
| `Policy.Propose` | propose policies (an ArchiveAdmin approves each in the portal) |
| `Recall.Request` | request recalls |
| `Archive.Read` | read the catalog, events and usage |
| `Archive.Adopt` | register sources and run migrations |

The owner is the person in your tenant answerable for the client. A proposal from the client is refused
(`403 owner_lacks_access`) unless its owner can see the dataset: an ArchiveAdmin or DataSteward who has
signed in to the portal in the last 30 days, or an Owner of one of the dataset's shares.

First give the client's application its scopes as application roles on the API (an Entra
administrator, as in step 30):

```bash guide-step=80 on=workstation
. ./omnifile.env && . ./omnifile.state.env
[ -n "$OMNI_API_CLIENT_APP_ID" ] || { echo "no API client in omnifile.env: section 18 skipped"; exit 0; }
roles=()
for s in $(tr ',' ' ' <<<"$OMNI_API_CLIENT_SCOPES"); do roles+=("$s=app:$OMNI_API_CLIENT_APP_ID"); done
docs/guide/tools/assign-app-roles.sh --app-id "$OMNI_WEB_CLIENT_ID" "${roles[@]}"
```

Success: `<client> now has <scope>` for each scope (or `already has` on a rerun). Then register it, as
an ArchiveAdmin; tick each of its scopes, and pick its owner by name:

```text guide-step=81 on=portal manual=yes
open /settings/api-clients
click Register client
fill Application (client) id = <value:OMNI_API_CLIENT_APP_ID>
fill Display name = <value:OMNI_API_CLIENT_NAME>
choose Scopes = <value:OMNI_API_CLIENT_SCOPES>
choose Owner = <value:OMNI_API_CLIENT_OWNER>
click Register client
expect <value:OMNI_API_CLIENT_NAME> is registered.
expect How a client signs in
```

Success: the client is listed, `active`. **How a client signs in**, below the list, shows what the
client's operator needs: the token endpoint, the scope to request and the token audiences.

The recommended way for a client to sign in is with its own Entra app credentials: a certificate on
its own app registration, so no secret is shared. Only for a client that can't do that, make it an
OmniFile API key on the client's page:

```text guide-step=82 on=portal manual=yes
open /settings/api-clients
click <value:OMNI_API_CLIENT_NAME>
click Create key
fill Expires after (days) = 90
click Create key
expect Your new API key
click Copy key
```

The key is shown once: give it to the client's operator over a channel you trust, and close the
window. The client sends it only as `Authorization: Bearer <key>`, never in a URL. It expires after the
days you chose (at most 365); rotate it on the client's page before then, and revoke it there if it
leaks. Its scopes are within the client's.
