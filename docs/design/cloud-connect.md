# Connecting Azure and AWS from the portal

**Status: BUILT against fakes (WP19, 2026-11-05).** The owner's feedback of 2026-11-04: Settings › Identity didn't say how to set up Azure, and AWS took a pasted ARN and a hand-made trust policy. This note is the design for four flows:

1. **Connect Azure** (Settings › Azure): an ArchiveAdmin signs in to Azure Resource Manager, picks a subscription and resource groups, and the service gives its own managed identities a few least-privilege roles there. Then the service finds the storage accounts, VNets and private endpoints, and the admin confirms them.
2. **Connect AWS** (Settings › AWS): "Launch in AWS" opens a CloudFormation stack that makes the role, the event queue and the Batch role. The service then assumes the new role with its web identity, reads the buckets, and fills in the account's settings.
3. **Deploy Storage Mover** (Settings › Storage Mover): the worker creates the Storage Mover and its project with the role from step 1, and the page walks the admin through the on-premises agent until it registers.
4. **Scan source** (New migration): a capped, read-only listing of the S3 prefix, which fills the wizard with prefixes, the storage-class mix, a restore estimate and suggested waves.

Nothing here adds a key, a secret or a stored admin token. Items marked **VERIFY** need a real account; they are lines in `docs/milestones/real-azure-checks.md` (RA-43 to RA-52) and `real-aws-checks.md` (RW-33 to RW-40).

## 1. Connect Azure

### 1.1 The steps the admin sees

1. **Sign in to Azure.** The portal asks the service for a sign-in link and the browser goes to Entra. The admin signs in (the same account as the portal session) and consents to "Azure Service Management" for the portal app.
2. **Choose where.** The service has listed the tenants, subscriptions and resource groups the admin can see. The admin picks one subscription, the resource groups that hold the client's storage and network, and optionally one resource group for Storage Mover.
3. **Check the grants.** The page shows every grant the service will make: the role, every action in it, the scope and the identity that gets it. Nothing is made yet.
4. **Make the grants.** A second sign-in. The service checks the admin may make role assignments on each scope, then makes exactly the grants shown. Each one is audited and listed with a Remove button.
5. **Discover.** The service reads, with its own identity, the storage accounts, VNets, private endpoints and storage movers in those resource groups, and shows them.
6. **Confirm.** The admin ticks what OmniFile should use. The confirmation is audited, and the Storage page can register a ticked account as a backend.

### 1.2 Why two sign-ins, and no stored token

The portal is a backend-for-frontend (portal-api.md §2.1): the browser never holds a token, and `web` already redeems codes as a confidential client (a client assertion from its managed identity; a secret only in dev mode). The connect flow uses the same machinery with a second redirect URI, `/auth/azure/callback`:

- `POST /v1/settings/azure/sign-in` records an operation (`catalog.azure_connect_op`): its purpose (`list`, `grant` or `remove`), the grants it covers, a PKCE verifier, a nonce, the admin's identity and Entra object id, and a 10-minute expiry. It sets a `__Host-` cookie with the state and returns the authorize URL. The scope is `openid https://management.azure.com/user_impersonation`, with `login_hint` set to the admin's sign-in name. A `grant` or `remove` also sends `max_age=600`, so it counts as a fresh sign-in.
- `/auth/azure/callback` checks the state against the cookie and the row, redeems the code, and checks the ID token (signature, issuer, tenant, nonce, and that `oid` is the object id that started the operation). Then it does the operation's work with the access token and redirects back to Settings › Azure.
- The access token lives only for the length of that request. No refresh token is asked for (no `offline_access`), nothing is cached, and the verifier is deleted when the code is redeemed. The plan the callback carries out is the one stored when the operation started. Nothing in the callback's query can change it.

So step 1 (list) and step 4 (grant) are two sign-ins. With Entra single sign-on, the second one usually needs no typing.

The one-time delegated permission: the portal app registration gets the delegated **Azure Service Management `user_impersonation`** permission (`deploy/entra/register.sh`), and the redirect URI `<origin>/auth/azure/callback`. **VERIFY** (RA-43): a user can consent to it unless the tenant blocks user consent. In that case a tenant administrator grants it once, from the consent link on the Identity page.

### 1.3 Tenants

The page lists every tenant the admin's token can see (`GET /tenants`), but only the deployment's own tenant can be chosen. The service's managed identities live in that tenant, and Azure assigns roles only to principals of the subscription's own tenant. The other tenants are shown with that reason. A client with subscriptions in another tenant needs Azure Lighthouse, which is out of scope.

### 1.4 The grants

Each grant is a custom role that the admin's token creates, scoped to one resource group, plus one role assignment to one service identity. The names and ids are fixed by the scope, so a second run updates the same role and assignment, and removing finds them again:

- role definition id: `uuid5(OMNIFILE_NS, "<purpose>|<scope>")`;
- role assignment name: `uuid5(OMNIFILE_NS, "<purpose>|<scope>|<principal object id>")`;
- role name: `OmniFile <purpose name> (<resource group>, <first 8 of the subscription id>)` (custom role names are unique per tenant).

| Purpose | Identity | Scope | Actions (control plane only) |
|---|---|---|---|
| `discover`: **OmniFile Discovery Reader** | `web` | each chosen resource group | `Microsoft.Storage/storageAccounts/read`, `…/blobServices/read`, `Microsoft.Network/virtualNetworks/read`, `…/virtualNetworks/subnets/read`, `Microsoft.Network/privateEndpoints/read`, `…/privateEndpoints/privateDnsZoneGroups/read`, `Microsoft.Network/privateDnsZones/read`, `…/privateDnsZones/virtualNetworkLinks/read`, `Microsoft.Authorization/roleAssignments/read`, `Microsoft.Authorization/roleDefinitions/read`, `Microsoft.StorageMover/storageMovers/read`, `…/agents/read`, `…/projects/read`, `…/endpoints/read` |
| `storage_mover`: **OmniFile Mover Deployer** | `worker` | the Storage Mover resource group | `Microsoft.StorageMover/storageMovers/read`, `…/write`, `…/projects/read`, `…/projects/write`, `…/agents/read`, `…/endpoints/read`, and the migration engine's job actions (`…/projects/jobDefinitions/read`, `…/write`, `…/startJob/action`, `…/stopJob/action`, `…/jobRuns/read`) |

What is never granted: no `dataActions` (no blob is read or written through these roles), no `listKeys` or other key actions, no `delete`, and no `Microsoft.Authorization/*/write`. So no service identity can grant itself more. The data roles on the service's own storage accounts stay in the Bicep deployment (`deploy/azure/modules/roles.bicep`), and the Storage page's checks still report them. The Storage Mover resource group also gets a Discovery Reader for `web`, so the page can show the agents.

The service identities' object ids come from `OMNIFILE_SERVICE_PRINCIPAL_IDS` (`web=…,worker=…`), which the deployment sets. If one is missing, the plan says so and the grant is refused.

**What the admin needs:** `Microsoft.Authorization/roleDefinitions/write` and `roleAssignments/write` on each scope: Owner or User Access Administrator. Before writing anything, the callback reads `GET {scope}/providers/Microsoft.Authorization/permissions` with the admin's token. If any scope lacks either action, it makes nothing and says which scope and which action. **VERIFY** (RA-44): that answer's shape and its `notActions`.

**Order and failure.** For each grant: PUT the role definition, then PUT the assignment (`principalType: ServicePrincipal`, which avoids the replication wait for a new principal). A new custom role can take a moment to replicate, so a `RoleDefinitionDoesNotExist` answer is retried three times, 2 s apart (**VERIFY**, RA-45). If a write fails, the grants already made are recorded and audited, so they show and can be removed. The operation ends `failed` with the reason.

**Removing.** Remove is also an operation with its own sign-in: DELETE the assignment, then DELETE the role definition when no other active grant uses it. A grant someone removed in the Azure portal answers 404, which counts as removed.

### 1.5 Discovery and confirmation

`POST /v1/settings/azure/discover` reads, with `web`'s own managed identity (Settings' `Probes`, as every other live check), per granted resource group:

- `Microsoft.Storage/storageAccounts`: name, location, SKU (ZRS or not), kind, hierarchical namespace, public network access, the default network rule;
- `Microsoft.Network/virtualNetworks`: address space and subnets;
- `Microsoft.Network/privateEndpoints`: subnet, target resource and group id (`blob`, `dfs`, …), and connection state;
- `Microsoft.StorageMover/storageMovers`.

For each approved blob private endpoint it also reads the endpoint's own DNS zone group, and the VNet links of the zones in it. Then it joins them. A storage account is **private** (decision 22) only when all three hold:
- **an approved `blob` private endpoint.** A connection with no status counts as not approved.
- **public access closed:** `publicNetworkAccess` Disabled, or the default action Deny with no IP rule and no VNet rule.
- **the DNS zone linked, for that account:** the account's own approved blob endpoint has a DNS zone group with a `privatelink.blob` zone, and that zone is linked to the service's VNet (`OMNIFILE_SERVICE_VNET_ID`, which the deployment sets). So the service resolves that account to its private address. A linked zone elsewhere in the tenant vouches for no other account (follow-up of #64).

Any other account is marked "not reachable privately" and can't be ticked for use. Without the VNet id, no account is private, and the page says why (review of #64). **VERIFY** (RA-46): the API versions and field names.

`POST /v1/settings/azure/confirm` records the ticked accounts, VNets and endpoints (by resource id) with who and when, in the audit log. The confirmation doesn't change where the service sends requests: registering a backend still goes through Storage's add form and the CLI's code path (`settings/storage.py`). The confirmed accounts are offered there, filled in.

### 1.6 The Identity tab

Settings › Identity now starts with plain-language steps, each with a status:

- **Entra ID is for people and the cloud:** portal sign-in, the app roles (ArchiveAdmin, DataSteward, User) given to AD groups synced to Entra, Teams cards, and the service's managed identities in Azure.
- **Active Directory is for the edges:** every edge joins AD with winbind and checks every NFS and SMB access against AD groups and POSIX ACLs (decision 28, `access-control.md`). Group SIDs in people's tokens tie the two together.
- Done or to do: the tenant and app registration, admin consent, the app roles held by groups, the `onprem_sid` claim and group SIDs in your token, Azure connected, at least one edge joined to AD.

The tenant, app, consent link, Graph check, role table and "Who am I" stay below, unchanged.

## 2. Connect AWS

### 2.1 The steps the admin sees

1. **Launch in AWS.** The button opens the CloudFormation console's quick-create page with OmniFile's template and this deployment's values filled in: the tenant, the `api://` audience and the service identities' object ids. The admin signs in to AWS (their own credentials, in their browser; OmniFile never sees them), names the source buckets, ticks the IAM acknowledgement and creates the stack.
2. **Turn on EventBridge for each source bucket** (bucket Properties, "Amazon EventBridge: On"). The stack can't do this: CloudFormation can't change a bucket it didn't make, and OmniFile never writes a source's configuration (decision 40). Events don't flow at once: AWS starts sending a bucket's events about five minutes after the setting is turned on, and a change made before then sends none (real AWS, 2026-11-14; AWS documents "around five minutes"). The step's text says so. "EventBridge: On" in the fetch's bucket list is the bucket's setting, not proof that an event has arrived.
3. **Fetch.** Back in the portal, the admin enters the account id and region (and, with private networking, the stack's STS and S3 endpoint outputs) and presses Fetch. The worker assumes the new role with its web identity, reads the buckets, and checks the queue.
4. **Confirm.** The page shows the account's entry for `OMNIFILE_AWS_ACCOUNTS`, filled in, and the buckets with their region, versioning and EventBridge status. The admin confirms. The entry is recorded and shown with where to set it.

The fallback for step 3: paste the stack's outputs (the console's Outputs tab, `aws cloudformation describe-stacks` JSON, or `Key=Value` lines). The service fills in the same entry without calling AWS.

### 2.2 The template

`deploy/aws/cloudformation/omnifile-connect.yaml` (a byte-identical copy ships in the service package, checked by a test). Parameters: `TenantId`, `Audience`, `ServiceSubjects`, `SourceBuckets`, `DeploymentId` (a short name used in every resource name), `CreateOidcProvider` (no if the account already has the provider for this tenant), and optional `VpcId`, `SubnetIds` and `EndpointClientCidr`, which create the S3, STS and SQS interface endpoints. `ServiceSubjects` and `SourceBuckets` are checked value by value (`AllowedPattern`), so nothing can break the trust policy's JSON. `EndpointClientCidr` has no default: the portal fills in the service's VNet range (`OMNIFILE_SERVICE_VNET_CIDR`), and a template rule requires it when `VpcId` is given. Every ARN names its partition (`AWS::Partition`, or a choice between `aws`, `aws-cn` and `aws-us-gov` where CloudFormation can't substitute).

It makes:

- **The IAM OIDC provider** for `https://sts.windows.net/<tenant>/` with the audience as its client id (when `CreateOidcProvider`).
- **The service role** `omnifile-service-<DeploymentId>`. Its trust policy is the one `settings/aws.py` shows: only `sts:AssumeRoleWithWebIdentity`, only from that provider, only for the audience and the listed subjects. A test checks the template's trust policy equals the page's. Its permissions:
  - read the source buckets: `s3:GetObject`, `GetObjectVersion`, `GetObjectAttributes`, `ListBucket`, `ListBucketVersions`, `GetBucketLocation`, `GetBucketVersioning`, `GetBucketNotification`, `GetInventoryConfiguration`, and `s3:RestoreObject` (restores are allowed, decision 15);
  - **an explicit deny of every S3 write outside the ops bucket** (decision 40; review of #64). It covers `s3:Put*`, `s3:Delete*`, `s3:Replicate*`, `s3:Bypass*`, `s3:AbortMultipartUpload` and `s3:ObjectOwnerOverrideToBucketOwner`, with `NotResource` the ops bucket and its objects. A write through an access point or a Multi-Region Access Point is denied too, not only through the bucket's own ARN. A deny wins over any other policy someone adds to the role. `s3:RestoreObject` isn't a write here: it is allowed explicitly, since OmniFile may always request restores;
  - `s3:ListAllMyBuckets` (the fetch's bucket list);
  - Batch Operations: `s3:CreateJob`, `DescribeJob`, `ListJobs`, `UpdateJobStatus`, `UpdateJobPriority`, and `iam:PassRole` for the Batch role and no other. The statement carries no `iam:PassedToService` condition: with one, real AWS denied `CreateJob` its `iam:PassRole` (2026-11-14; `StringEquals` and `StringEqualsIfExists` alike), and without it the job ran. The Batch role's trust policy, below, is what keeps it to Batch Operations;
  - the ops bucket (manifests, reports, the staging prefix): get, put, delete and list there only;
  - the event queue: receive, delete, change visibility, get attributes;
  - `cloudformation:DescribeStacks` on this stack only.
- **The Batch role** `omnifile-batch-<DeploymentId>`, trusted by `batchoperations.s3.amazonaws.com` for this account's jobs only (`aws:SourceAccount`): restore and read on the sources, read manifests from and write reports to the ops bucket, and the same deny of writes elsewhere.
- **The ops bucket** `omnifile-ops-<DeploymentId>-<account>`: public access blocked, owner-enforced, SSE-S3, TLS only (a bucket policy denies `aws:SecureTransport` false). A lifecycle rule aborts incomplete multipart uploads after a day. The service role and the S3 endpoint's policy allow `s3:AbortMultipartUpload` there, so an interrupted staging copy or manifest upload is cleaned up.
- **The interface endpoints** (with a VPC), each with a least-privilege policy, never full access:
  - S3: the reads, restores, bucket listing and Batch calls, and writes only to the ops bucket, all for this account's principals.
  - STS: only `AssumeRoleWithWebIdentity` of the service role.
  - SQS: only the event queue.

  Their security group admits 443 from the service's VNet range only.
- **The event queue** `omnifile-events-<DeploymentId>`, SSE, 14 days' retention, with a policy that lets only the EventBridge rule send to it, and **the rule**: S3 events (`Object Created`, `Object Deleted`, `Object Restore Initiated/Completed/Expired`, `Object Storage Class Changed`, `Object Access Tier Changed`) for the source buckets.
- **Outputs:** `RoleArn`, `BatchRoleArn`, `EventQueueUrl`, `EventQueueArn`, `OpsBucket`, `AccountId`, `Region`, `SourceBuckets`, and with a VPC `S3EndpointDns`, `StsEndpointDns`, `SqsEndpointDns`.

**The quick-create link.** CloudFormation's quick-create takes a `templateURL` in S3, not one the service hosts. So the link uses `OMNIFILE_AWS_CONNECT_TEMPLATE_URL`, where each release publishes the template (a release task, like the source offer; **VERIFY**, RW-33). Without it, the page offers the template for download and the console's "Create stack, Upload a template file" link, with the same parameter values to type. The link carries the values as `param_<Name>`; `stackName` is `omnifile-<DeploymentId>`. **VERIFY** (RW-34): the link's form and that it opens with the values filled in.

### 2.3 What stays an operator setting, and why

The connect flow doesn't write the account into the catalog for the backend to use. `backends/s3/config.py` keeps "where requests go and which identity is used" in operator settings, never in a catalog row (the M5 review's B1). A stolen ArchiveAdmin session could otherwise point the service's web identity at an account and endpoints of its choosing. So the confirm step records the entry (`catalog.aws_connection`, audited) and shows it as the exact JSON for the deployment's `awsAccounts` parameter (`OMNIFILE_AWS_ACCOUNTS`). Once it is deployed, the page shows the account as configured. The entry is checked by `AwsDeployment.from_mapping` before it is shown, so it is one the worker will load.

The entry carries the deployment's mode: `private_network` true with the interface endpoints, or false in a deployment without private networking. An account defaults to private, so without this a public deployment's entry would be refused for lacking endpoints (review of #64).

The **fetch** does use the admin's input once: the account id, region and (in private mode) the STS and S3 endpoints. Those pass the same checks as the operator's settings (`check_endpoint`: https, a `*.vpce.amazonaws.com` host with private networking, no loopback). The session is used only for read calls (`ListBuckets`, `GetBucketLocation`, `GetBucketVersioning`, `GetBucketNotificationConfiguration`, `GetQueueAttributes`, and `DescribeStacks` without private networking), and its credentials are never stored. The worst a forged fetch can do is send the worker's web-identity token to an AWS STS endpoint, which issues credentials only to a role whose trust policy names this tenant, audience and subject.

## 3. Deploy Storage Mover

Decision 20 stands: Azure Storage Mover copies the restored objects, and the migration engine defines and runs its jobs (`migrate/storage_mover.py`). This adds the setup.

1. Connect Azure with a Storage Mover resource group (§1.4).
2. **Deploy:** `POST /v1/settings/storage-mover/deploy` (name, project, location) queues a task for the worker. With its own managed identity and the Mover Deployer role, the worker PUTs `Microsoft.StorageMover/storageMovers/{name}`, then `…/projects/{project}`, and reads them back. **VERIFY** (RA-47): the API version and that these two PUTs need nothing else.
3. **The agent.** A Storage Mover agent is a VM the client runs on-premises. The page shows: download the agent image from Microsoft (link to Microsoft's agent deployment guide), create the VM on Hyper-V or VMware (the guide's sizes), sign in to its console, choose "Register", and enter the values the page shows (subscription, resource group, Storage Mover name, tenant). The agent registers with Azure Arc. **VERIFY** (RA-48): the download link and the console's steps.
4. **Live status:** the page reads the mover's agents every 15 s with `web`'s identity (the Discovery Reader) until one is `Online`.
5. **Endpoints and the setting.** The source endpoints (one per S3 bucket, through an Azure Arc multicloud connector) and target endpoints stay a setup-guide step, as `deploy/azure/README.md` step 10 says. The page shows the `OMNIFILE_STORAGE_MOVER` value to set (`storageMoverConfig`), filled in with the names just made.

Without it, migrations still run with `copy: external`.

## 4. Scan source

New migration's source step has a **Scan** button once a backend, bucket and prefix are chosen.

- `POST /v1/source-scans` (ArchiveAdmin, signed in) queues a scan task for the worker. The worker uses the same readers as seeding (`sources/run.py reader_for`): the listing, or the S3 Inventory if the wizard named one. They only list and read the inventory's own bucket. So the scan makes no write anywhere, and nothing on the source (decision 40). A test runs a scan over the S3 fake and checks that every call it saw was a list or a get.
- **Caps:** at most `max_objects` (default 2,000,000; at most 10,000,000) and `max_seconds` (default 300; at most 1,800). The worker reads one batch of 10,000 objects per queue message and writes the totals after each, so the page shows progress as it goes. At a cap it stops and says `capped`, and the totals are of what it saw. 2,000,000 objects take 2,000 LIST requests: about $0.01 at S3's list price (**VERIFY**, RW-35, with the client's prices).
- **What it finds:**
  - per first-level prefix under the scanned one: objects, bytes, and the objects and bytes needing a restore;
  - the storage-class mix: objects and bytes per class, marked "restore needed" for `GLACIER` and `DEEP_ARCHIVE` (`WAVE_CLASSES`) and for Intelligent-Tiering's archive tiers when the listing says so;
  - **the restore estimate:** Bulk restores (`WAVE_SPEED`), in waves of at most 100,000 objects (`WAVE_MAX`). The time per wave is AWS's stated Bulk time per class (Flexible up to 12 h, Deep Archive up to 48 h; `sim/capabilities`). The 1,000 restore requests a second that user recalls share is the request budget. The cost uses the S3 price table if an ArchiveAdmin set one (Settings › Prices): retrieval per GB and requests per 1,000 for each class. Without a table the page says no prices are set, and shows only counts and time;
  - objects over 10 GiB are counted apart, and the page promises no time for them (the 10 GiB rule: never promise a recall time above 10 GB);
  - **suggested waves:** prefixes packed in order into waves of at most 100,000 archived objects and 50 TiB. Prefixes with nothing to restore go in a first "copy only" group.
- **Filling the wizard:** "Use this prefix" sets the prefix field from a row, and the review step shows the scan's totals, the estimate and the waves. A scan is kept 7 days and is visible to ArchiveAdmins.

## 5. Threat model notes (to fold into `threat-model.md`)

- **T-CC1, a stored admin token.** None: the code is redeemed and used in one request, no refresh token is asked for, and the verifier is deleted. A database leak gives no Azure access.
- **T-CC2, login CSRF and mix-up.** The state is in a `__Host-` cookie and the row, the ID token's nonce is checked, and its `oid` must be the admin who started the operation. A link someone else made fails at the cookie. The callback refuses another tenant's token.
- **T-CC3, the delegated scope is broad.** `user_impersonation` carries all of the admin's Azure rights. The service makes only the calls of the plan stored when the operation began: GETs while listing, and in a grant only PUTs of the listed roles and assignments and nothing else. A test checks the exact calls.
- **T-CC4, escalation through the service identities.** The roles hold reads, plus Storage Mover writes in one resource group for the worker. No identity gets an authorization write, a key, or a data action, so a compromised `web` or `worker` can't widen its own access.
- **T-CC5, a stolen ArchiveAdmin session.** It can grant these fixed roles only where the admin's Azure account may already assign roles, and only after a fresh Entra sign-in (`max_age=600`). It can't change where the S3 backend sends requests (§2.3).
- **T-CC6, AWS.** No keys. The role trusts only this tenant's tokens for one audience and the listed subjects, and denies every write on the sources. The fetch's input is checked like an operator setting and used only for reads.
- **T-CC7, the scan.** It reads under the backend's role, ArchiveAdmins only, with caps on objects and time. Its results hold key prefixes, which ArchiveAdmins already see on the migration pages.

## 6. Contract

`portal-api.md` §5.20 lists the endpoints; `service/openapi/portal-api.yaml` has their shapes. The new catalog tables are in migration 0044: `azure_connect_op` (web only, as `oidc_login`: it holds a verifier), `azure_connection`, `azure_grant`, `aws_connection` and `cloud_task` (the worker's tasks: AWS fetch, Storage Mover deploy, source scan), with the `cloud_task` queue.
