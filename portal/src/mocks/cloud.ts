import { http, HttpResponse, type JsonBodyType } from "msw";
import type { components } from "../api/schema.gen";
import type { MockDb } from "./db";

/**
 * MSW handlers for Connect Azure, Connect AWS, Deploy Storage Mover and source
 * scans (docs/design/cloud-connect.md). The catch-all in handlers.ts runs
 * first, so CSRF and Idempotency-Key are checked there.
 *
 * A Connect Azure sign-in is a top-level navigation: in the service it goes to
 * Entra and comes back through /auth/azure/callback. Here the authorize URL
 * points straight back to /settings/azure?op=<id>, and the operation's work is
 * done when it starts. In `dev:mock` that navigation reloads the page, so the
 * state is kept in sessionStorage there (`persistCloudMock`); tests keep it in
 * memory, per mock database.
 *
 * Workers' tasks (an AWS fetch, a Storage Mover deployment, a source scan)
 * move one step each time they are read, so pages show them progress.
 */

type S = components["schemas"];
type Persona = MockDb["persona"];
type Listing = NonNullable<S["AzureSettings"]["listing"]>;
type Choice = NonNullable<S["AzureSettings"]["choice"]>;
type Confirmed = NonNullable<S["AzureSettings"]["confirmed"]>;
type LastOp = NonNullable<S["AzureSettings"]["last_op"]>;

interface CloudState {
  nextId: number;
  lastOp: LastOp | null;
  listing: Listing | null;
  choice: Choice | null;
  grants: S["AzureGrant"][];
  discovered: S["AzureDiscovery"] | null;
  confirmed: Confirmed | null;
  connections: S["AwsConnection"][];
  tasks: Record<string, { task: S["CloudTask"]; polls: number; params: Record<string, unknown> }>;
  scans: Record<string, { scan: S["SourceScan"]; polls: number }>;
  /** The latest Storage Mover deployment's task, and reads of the mover since it finished. */
  mover: { taskId: string; agentReads: number } | null;
  /** OMNIFILE_AWS_CONNECT_TEMPLATE_URL: null, and there is no quick-create link. */
  templateUrl: string | null;
}

const TENANT = "8f3a2b1c-0d9e-4f8a-b7c6-d5e4f3a2b1c0";
const PORTAL_APP = "1d2c3b4a-5e6f-4a7b-8c9d-0e1f2a3b4c5d";
const AUDIENCE = `api://${PORTAL_APP}`;
const SUBJECT = "7a1b2c3d-4e5f-4a6b-8c7d-9e0f1a2b3c4d";
const FABRIKAM = "99999999-9999-4999-8999-999999999999";
export const DEMO_SUBSCRIPTION = "00000000-0000-4000-8000-0000000000a1";
const IDENTITIES = {
  web: "3c1e2d4f-5a6b-4c7d-8e9f-0a1b2c3d4e5f",
  worker: "7d2f3e4a-5b6c-4d7e-8f9a-0b1c2d3e4f5a",
};
const TEMPLATE_URL = "https://omnifile-releases.s3.amazonaws.com/connect/v1/omnifile-connect.yaml";
const KNOWN_ACCOUNT = "123456789012";

const ROLES = {
  discover: {
    principal: "web" as const,
    name: "OmniFile Discovery Reader",
    actions: [
      "Microsoft.Storage/storageAccounts/read",
      "Microsoft.Storage/storageAccounts/blobServices/read",
      "Microsoft.Network/virtualNetworks/read",
      "Microsoft.Network/virtualNetworks/subnets/read",
      "Microsoft.Network/privateEndpoints/read",
      "Microsoft.Network/privateDnsZones/read",
      "Microsoft.Network/privateDnsZones/virtualNetworkLinks/read",
      "Microsoft.Authorization/roleAssignments/read",
      "Microsoft.Authorization/roleDefinitions/read",
      "Microsoft.StorageMover/storageMovers/read",
      "Microsoft.StorageMover/storageMovers/agents/read",
      "Microsoft.StorageMover/storageMovers/projects/read",
      "Microsoft.StorageMover/storageMovers/endpoints/read",
    ],
  },
  storage_mover: {
    principal: "worker" as const,
    name: "OmniFile Mover Deployer",
    actions: [
      "Microsoft.StorageMover/storageMovers/read",
      "Microsoft.StorageMover/storageMovers/write",
      "Microsoft.StorageMover/storageMovers/projects/read",
      "Microsoft.StorageMover/storageMovers/projects/write",
      "Microsoft.StorageMover/storageMovers/agents/read",
      "Microsoft.StorageMover/storageMovers/endpoints/read",
      "Microsoft.StorageMover/storageMovers/projects/jobDefinitions/read",
      "Microsoft.StorageMover/storageMovers/projects/jobDefinitions/write",
      "Microsoft.StorageMover/storageMovers/projects/jobDefinitions/startJob/action",
      "Microsoft.StorageMover/storageMovers/projects/jobDefinitions/stopJob/action",
      "Microsoft.StorageMover/storageMovers/projects/jobDefinitions/jobRuns/read",
    ],
  },
};
type Purpose = keyof typeof ROLES;

const AGENT_STEPS = [
  "Download the Storage Mover agent image from Microsoft's agent deployment guide: https://learn.microsoft.com/azure/storage-mover/agent-deploy",
  "Create a VM from it on Hyper-V or VMware, near the data (the guide lists the sizes: at least 8 vCPUs and 16 GiB of memory), with outbound HTTPS to Azure.",
  "Sign in to the VM's console with the guide's default account and change its password.",
  "Choose Register, and give the subscription, resource group and Storage Mover name shown here, and the tenant. Sign in with an account that may register agents there.",
  "This page shows the agent as Online once it has registered (it checks every 15 s).",
];

const iso = (ms: number) => new Date(ms).toISOString();
const json = (body: unknown, init?: ResponseInit) => HttpResponse.json(body as JsonBodyType, init);

function problem(status: number, code: string, detail: string, extra: object = {}) {
  return HttpResponse.json(
    {
      type: `urn:omnifile:error:${code}`,
      title: code.replace(/_/g, " "),
      status,
      code,
      detail,
      retryable: false,
      retry_after_s: null,
      request_id: "mock-cloud",
      ...extra,
    } as JsonBodyType,
    { status, headers: { "Content-Type": "application/problem+json" } },
  );
}
const invalid = (field: string, message: string) =>
  problem(400, "invalid_request", `${field}: ${message}`, { errors: [{ field, message }] });
/** The service's _precondition: a step that has to come first (422 dependency_failed). */
const precondition = (detail: string) => problem(422, "dependency_failed", detail);

/** A stable GUID-shaped id from a string (the service uses uuid5). */
function guid(text: string): string {
  let h1 = 0x811c9dc5;
  let h2 = 0x01000193;
  for (let i = 0; i < text.length; i++) {
    h1 = Math.imul(h1 ^ text.charCodeAt(i), 16777619) >>> 0;
    h2 = Math.imul(h2 + text.charCodeAt(i), 2246822519) >>> 0;
  }
  const hex = (h1.toString(16).padStart(8, "0") + h2.toString(16).padStart(8, "0")).repeat(2);
  return `${hex.slice(0, 8)}-${hex.slice(8, 12)}-5${hex.slice(13, 16)}-8${hex.slice(17, 20)}-${hex.slice(20, 32)}`;
}

// --- state, per mock database (and kept across reloads in dev:mock) -------------------------

const STORAGE_KEY = "omnifile.mockCloud";
let persist = false;
const states = new WeakMap<MockDb, CloudState>();

/** dev:mock only: keep the cloud state across the reload a sign-in navigation makes. */
export function persistCloudMock(): void {
  persist = true;
}

function fresh(): CloudState {
  return {
    nextId: 700,
    lastOp: null,
    listing: null,
    choice: null,
    grants: [],
    discovered: null,
    confirmed: null,
    connections: [],
    tasks: {},
    scans: {},
    mover: null,
    templateUrl: TEMPLATE_URL,
  };
}

function load(db: MockDb): CloudState {
  let s = states.get(db);
  if (s) return s;
  s = fresh();
  if (persist) {
    try {
      const raw = sessionStorage.getItem(STORAGE_KEY);
      if (raw) s = { ...s, ...(JSON.parse(raw) as Partial<CloudState>) };
    } catch {
      // storage blocked or the saved state unreadable: start fresh
    }
  }
  states.set(db, s);
  return s;
}

function save(s: CloudState): void {
  if (!persist) return;
  try {
    sessionStorage.setItem(STORAGE_KEY, JSON.stringify(s));
  } catch {
    // storage blocked: the state lasts until the next reload
  }
}

// --- Connect Azure --------------------------------------------------------------------------

const scopeOf = (rg: string, sub = DEMO_SUBSCRIPTION) =>
  `/subscriptions/${sub}/resourceGroups/${rg}`;
const rgOf = (scope: string) => scope.split("/")[4] ?? scope;

function listing(by: string | null): Listing {
  return {
    listed_at: iso(Date.now()),
    listed_by: by,
    tenants: [
      { tenant_id: TENANT, display_name: "Contoso", selectable: true, reason: null },
      {
        tenant_id: FABRIKAM,
        display_name: "Fabrikam",
        selectable: false,
        reason:
          "Not OmniFile's tenant: Azure assigns roles only to identities of a subscription's own tenant. Another tenant needs Azure Lighthouse, which OmniFile doesn't set up.",
      },
    ],
    subscriptions: [
      {
        subscription_id: DEMO_SUBSCRIPTION,
        display_name: "Contoso production",
        tenant_id: TENANT,
        state: "Enabled",
      },
    ],
    resource_groups: ["rg-storage", "rg-network", "rg-migration"].map((name) => ({
      subscription_id: DEMO_SUBSCRIPTION,
      name,
      location: "westeurope",
    })),
    truncated: false,
  };
}

function plan(s: CloudState): S["AzureGrantPlan"][] {
  const c = s.choice;
  if (!c) return [];
  const wanted: [Purpose, string][] = c.resource_groups.map((rg) => ["discover", rg]);
  if (c.mover_resource_group) {
    if (!c.resource_groups.includes(c.mover_resource_group)) {
      wanted.push(["discover", c.mover_resource_group]);
    }
    wanted.push(["storage_mover", c.mover_resource_group]);
  }
  return wanted.map(([purpose, rg]) => {
    const role = ROLES[purpose];
    const scope = scopeOf(rg, c.subscription_id);
    const principalId = IDENTITIES[role.principal];
    const key = `${purpose}|${scope}|${principalId}`;
    const grant = s.grants.find(
      (g) => g.state === "active" && `${g.purpose}|${g.scope}|${g.principal_id}` === key,
    );
    return {
      key,
      purpose,
      role_name: `${role.name} (${rg}, ${c.subscription_id.slice(0, 8)})`,
      role_definition_id: guid(`${purpose}|${scope.toLowerCase()}`),
      scope,
      principal: role.principal,
      principal_id: principalId,
      actions: role.actions,
      state: grant ? "active" : "planned",
      grant_id: grant?.grant_id ?? null,
    };
  });
}

function azureView(s: CloudState): S["AzureSettings"] {
  const items = plan(s);
  const granted = items.length > 0 && items.every((i) => i.state === "active");
  const d = s.discovered;
  const step = (code: string, title: string, done: boolean, detail: string, attention = false) => ({
    code,
    title,
    status: done ? "done" : attention ? "attention" : "todo",
    detail,
  });
  return {
    tenant_id: TENANT,
    identities: [
      { name: "web", object_id: IDENTITIES.web },
      { name: "worker", object_id: IDENTITIES.worker },
    ],
    steps: [
      step(
        "identities",
        "The service's identities are known",
        true,
        "The deployment names the web and worker managed identities.",
      ),
      step(
        "sign_in",
        "Sign in to Azure",
        s.listing !== null,
        "Lists the tenants, subscriptions and resource groups you can see.",
      ),
      step(
        "choose",
        "Choose the subscription and resource groups",
        s.choice !== null,
        "Where the client's storage, network and Storage Mover are.",
      ),
      step(
        "grant",
        "Grant the service's identities their roles",
        granted,
        items.length
          ? `${items.filter((i) => i.state === "active").length} of ${items.length} grants made.`
          : "Shown before anything is made; a second sign-in makes them.",
      ),
      step(
        "discover",
        "Discover storage and network",
        d !== null && d.errors.length === 0,
        d
          ? `Found ${d.storage_accounts.length} storage accounts, ${d.virtual_networks.length} VNets and ${d.private_endpoints.length} private endpoints.`
          : "Reads what the service may now see, with its own identity.",
        !!d && d.errors.length > 0,
      ),
      step(
        "confirm",
        "Confirm what OmniFile uses",
        s.confirmed !== null,
        "Ticked storage accounts are offered on the Storage page.",
      ),
    ],
    listing: s.listing,
    choice: s.choice,
    plan: items,
    grants: s.grants,
    discovered: s.discovered,
    confirmed: s.confirmed,
    last_op: s.lastOp,
  };
}

function discovery(s: CloudState): S["AzureDiscovery"] {
  const granted = new Set(
    s.grants
      .filter((g) => g.state === "active" && g.purpose === "discover")
      .map((g) => rgOf(g.scope)),
  );
  const sub = `/subscriptions/${DEMO_SUBSCRIPTION}/resourceGroups`;
  const SERVICE_VNET =
    "/subscriptions/00000000-0000-4000-8000-0000000000aa/resourceGroups/rg-omnifile/providers/Microsoft.Network/virtualNetworks/omnifile-vnet";
  const sa = (rg: string, name: string) =>
    `${sub}/${rg}/providers/Microsoft.Storage/storageAccounts/${name}`;
  const vnet = `${sub}/rg-network/providers/Microsoft.Network/virtualNetworks/vnet-hub`;
  const accounts: S["AzureStorageAccount"][] = [
    {
      id: sa("rg-storage", "omnilive01"),
      name: "omnilive01",
      resource_group: "rg-storage",
      location: "westeurope",
      sku: "Standard_ZRS",
      kind: "StorageV2",
      zrs: true,
      hns: false,
      public_network_access: "Disabled",
      blob_private_endpoint: true,
      dns_linked: true,
      private: true,
    },
    {
      id: sa("rg-storage", "contososhared01"),
      name: "contososhared01",
      resource_group: "rg-storage",
      location: "westeurope",
      sku: "Standard_LRS",
      kind: "StorageV2",
      zrs: false,
      hns: false,
      public_network_access: "Enabled",
      blob_private_endpoint: false,
      dns_linked: true,
      private: false,
    },
  ];
  const deployed = s.mover ? s.tasks[s.mover.taskId] : undefined;
  const moverRg = (deployed?.params.resource_group as string | undefined) ?? "rg-migration";
  const moverName = (deployed?.params.name as string | undefined) ?? "contoso-mover";
  return {
    discovered_at: iso(Date.now()),
    service_vnet_id: SERVICE_VNET,
    blob_dns_zones: granted.has("rg-network")
      ? [
          {
            id: `${sub}/rg-network/providers/Microsoft.Network/privateDnsZones/privatelink.blob.core.windows.net`,
            linked_to_service: true,
          },
        ]
      : [],
    storage_accounts: accounts.filter((a) => granted.has(a.resource_group)),
    virtual_networks: granted.has("rg-network")
      ? [
          {
            id: vnet,
            name: "vnet-hub",
            resource_group: "rg-network",
            location: "westeurope",
            address_prefixes: ["10.20.0.0/16"],
            subnets: ["snet-endpoints", "snet-apps", "GatewaySubnet"],
          },
        ]
      : [],
    private_endpoints: granted.has("rg-network")
      ? [
          {
            id: `${sub}/rg-network/providers/Microsoft.Network/privateEndpoints/pe-omnilive01-blob`,
            name: "pe-omnilive01-blob",
            resource_group: "rg-network",
            subnet_id: `${vnet}/subnets/snet-endpoints`,
            target_id: sa("rg-storage", "omnilive01"),
            group_ids: ["blob"],
            status: "Approved",
          },
        ]
      : [],
    storage_movers: granted.has(moverRg)
      ? [
          {
            id: `${sub}/${moverRg}/providers/Microsoft.StorageMover/storageMovers/${moverName}`,
            name: moverName,
            resource_group: moverRg,
            location: "westeurope",
          },
        ]
      : [],
    errors: [],
  };
}

// --- Connect AWS ----------------------------------------------------------------------------

function names(account: string, dep: string, region: string) {
  return {
    role_arn: `arn:aws:iam::${account}:role/omnifile-service-${dep}`,
    batch_role_arn: `arn:aws:iam::${account}:role/omnifile-batch-${dep}`,
    ops_bucket: `omnifile-ops-${dep}-${account}`,
    queue_url: `https://sqs.${region}.amazonaws.com/${account}/omnifile-events-${dep}`,
  };
}

function entryOf(account: string, region: string, dep: string, outputs: Record<string, string>) {
  const n = names(account, dep, region);
  const entry: Record<string, unknown> = {
    region,
    role_arn: n.role_arn,
    event_queues: { "*": outputs.EventQueueUrl ?? n.queue_url },
    batch: { role_arn: n.batch_role_arn, manifest_bucket: n.ops_bucket },
    staging_bucket: n.ops_bucket,
  };
  const eps: Record<string, string> = {};
  if (outputs.S3EndpointDns) eps.s3 = `https://${outputs.S3EndpointDns}`;
  if (outputs.StsEndpointDns) eps.sts = `https://${outputs.StsEndpointDns}`;
  if (outputs.SqsEndpointDns) eps.sqs = `https://${outputs.SqsEndpointDns}`;
  if (Object.keys(eps).length) entry.endpoints = eps;
  return entry;
}

function stackOutputs(account: string, region: string, dep: string, buckets: string) {
  const n = names(account, dep, region);
  return {
    AccountId: account,
    Region: region,
    DeploymentId: dep,
    RoleArn: n.role_arn,
    BatchRoleArn: n.batch_role_arn,
    OpsBucket: n.ops_bucket,
    EventQueueUrl: n.queue_url,
    EventQueueArn: `arn:aws:sqs:${region}:${account}:omnifile-events-${dep}`,
    SourceBuckets: buckets,
    S3EndpointDns: `vpce-0a1b2c3d4e5f60718-s3.s3.${region}.vpce.amazonaws.com`,
    StsEndpointDns: `vpce-0a1b2c3d4e5f60719-sts.sts.${region}.vpce.amazonaws.com`,
    SqsEndpointDns: `vpce-0a1b2c3d4e5f6071a-sqs.sqs.${region}.vpce.amazonaws.com`,
  };
}

/** The console's Outputs tab (tab-separated), describe-stacks JSON, or Key=Value lines. */
function parseOutputs(text: string): Record<string, string> {
  const out: Record<string, string> = {};
  try {
    const parsed = JSON.parse(text) as {
      Stacks?: { Outputs?: { OutputKey: string; OutputValue: string }[] }[];
    };
    for (const o of parsed.Stacks?.[0]?.Outputs ?? []) out[o.OutputKey] = o.OutputValue;
    if (Object.keys(out).length) return out;
  } catch {
    // not JSON: read lines
  }
  for (const line of text.split(/\r?\n/)) {
    const m = /^\s*([A-Za-z0-9]+)\s*(?:=|\t+|\s{2,})\s*(\S+)/.exec(line);
    if (m?.[1] && m[2]) out[m[1]] = m[2];
  }
  return out;
}

function settingsJson(s: CloudState, account: string, entry: Record<string, unknown>) {
  const current: Record<string, unknown> = {
    [KNOWN_ACCOUNT]: {
      region: "eu-west-1",
      role_arn: `arn:aws:iam::${KNOWN_ACCOUNT}:role/omnifile-service`,
    },
  };
  for (const c of s.connections) current[c.account_id] = c.entry;
  current[account] = entry;
  const sorted = Object.fromEntries(Object.entries(current).sort(([a], [b]) => a.localeCompare(b)));
  return JSON.stringify(sorted, null, 2);
}

// --- tasks ----------------------------------------------------------------------------------

function newTask(s: CloudState, kind: string, params: Record<string, unknown>): S["CloudTask"] {
  const now = iso(Date.now());
  const task: S["CloudTask"] = {
    task_id: String(++s.nextId),
    kind,
    state: "pending",
    detail: null,
    progress: {},
    result: {},
    created_at: now,
    updated_at: now,
    finished_at: null,
  };
  s.tasks[task.task_id] = { task, polls: 0, params };
  return task;
}

function fetchResult(params: Record<string, unknown>): S["AwsFetchResult"] {
  const account = String(params.account_id);
  const region = String(params.region);
  const dep = String(params.deployment_id);
  const outputs = stackOutputs(account, region, dep, "acme-seismic,acme-wells");
  const endpoints = (params.endpoints ?? {}) as Record<string, string>;
  const entry = entryOf(account, region, dep, outputs);
  if (Object.keys(endpoints).length) {
    entry.endpoints = { ...(entry.endpoints as object | undefined), ...endpoints };
  }
  return {
    account_id: account,
    region,
    role_arn: names(account, dep, region).role_arn,
    buckets: [
      { name: "acme-seismic", region, versioning: "Enabled", eventbridge: true, in_stack: true },
      { name: "acme-wells", region, versioning: "Enabled", eventbridge: false, in_stack: true },
      { name: "acme-logs", region, versioning: null, eventbridge: null, in_stack: false },
    ],
    queue: { url: outputs.EventQueueUrl, ok: true },
    stack: { StackName: `omnifile-${dep}`, StackStatus: "CREATE_COMPLETE" },
    entry,
    outputs,
    problems: [],
  };
}

/** One step per read: pending, running, then done. */
function advanceTask(s: CloudState, id: string): S["CloudTask"] | null {
  const t = s.tasks[id];
  if (!t) return null;
  t.polls += 1;
  const now = iso(Date.now());
  if (t.task.state === "pending") {
    t.task = { ...t.task, state: "running", updated_at: now };
  } else if (t.task.state === "running") {
    if (t.task.kind === "aws_fetch") {
      t.task = {
        ...t.task,
        state: "done",
        detail: "Assumed the role and read 3 buckets and the event queue.",
        result: fetchResult(t.params),
        updated_at: now,
        finished_at: now,
      };
    } else {
      const p = t.params;
      t.task = {
        ...t.task,
        state: "done",
        detail: `Created storage mover ${String(p.name)} and project ${String(p.project)}.`,
        result: { storage_mover: p.name, project: p.project, config_value: moverConfig(p) },
        updated_at: now,
        finished_at: now,
      };
    }
  }
  save(s);
  return t.task;
}

function moverConfig(p: Record<string, unknown>): string {
  return JSON.stringify(
    {
      subscription_id: p.subscription_id,
      resource_group: p.resource_group,
      storage_mover: p.name,
      project: p.project,
      source_endpoints: { "<bucket>": "<source endpoint>" },
      target_endpoints: { "<account>/<container>": "<target endpoint>" },
    },
    null,
    2,
  );
}

// --- source scans ---------------------------------------------------------------------------

const GB = 1_000_000_000;

function scanResult(prefix: string): S["SourceScanResult"] {
  const p = (name: string) => `${prefix}${name}/`;
  const prefixes = [
    {
      prefix: p("2019"),
      objects: 61_204,
      bytes: 48_900 * GB,
      restore_objects: 61_204,
      restore_bytes: 48_900 * GB,
    },
    {
      prefix: p("2020"),
      objects: 38_118,
      bytes: 31_200 * GB,
      restore_objects: 35_002,
      restore_bytes: 29_700 * GB,
    },
    {
      prefix: p("2021"),
      objects: 27_450,
      bytes: 22_400 * GB,
      restore_objects: 9_310,
      restore_bytes: 7_150 * GB,
    },
    { prefix: p("logs"), objects: 14_022, bytes: 96 * GB, restore_objects: 0, restore_bytes: 0 },
  ];
  const sum = (k: "objects" | "bytes" | "restore_objects" | "restore_bytes", rows = prefixes) =>
    rows.reduce((a, r) => a + r[k], 0);
  const wave = (n: number, rows: typeof prefixes, copyOnly: boolean) => ({
    wave: n,
    prefixes: rows.map((r) => r.prefix),
    objects: sum("objects", rows),
    bytes: sum("bytes", rows),
    restore_objects: sum("restore_objects", rows),
    restore_bytes: sum("restore_bytes", rows),
    copy_only: copyOnly,
  });
  const [y19, y20, y21, logs] = prefixes as [
    (typeof prefixes)[0],
    (typeof prefixes)[0],
    (typeof prefixes)[0],
    (typeof prefixes)[0],
  ];
  return {
    objects: sum("objects"),
    bytes: sum("bytes"),
    prefixes,
    classes: [
      { storage_class: "STANDARD", objects: 16_890, bytes: 1_880 * GB, restore_needed: false },
      { storage_class: "STANDARD_IA", objects: 8_388, bytes: 6_066 * GB, restore_needed: false },
      { storage_class: "GLACIER", objects: 44_312, bytes: 36_850 * GB, restore_needed: true },
      { storage_class: "DEEP_ARCHIVE", objects: 61_204, bytes: 48_900 * GB, restore_needed: true },
    ],
    restore: {
      objects: sum("restore_objects"),
      bytes: sum("restore_bytes"),
      waves: 2,
      hours: 48,
      request_seconds: 105.5,
      cost: "2386.47",
      currency: "USD",
      prices_set: true,
      notes: [
        "Deep Archive takes up to 48 h with Bulk restores; Flexible Retrieval up to 12 h.",
        "Each restored copy is billed for the days it is kept, on top of the archived object.",
      ],
    },
    large_objects: { objects: 3, bytes: 41 * GB },
    waves: [wave(1, [logs], true), wave(2, [y19], false), wave(3, [y20, y21], false)],
  };
}

function advanceScan(s: CloudState, id: string): S["SourceScan"] | null {
  const x = s.scans[id];
  if (!x) return null;
  x.polls += 1;
  const r = scanResult(x.scan.prefix);
  if (x.scan.state === "pending" || x.scan.state === "running") {
    if (x.polls >= 3) {
      x.scan = {
        ...x.scan,
        state: "done",
        detail: `Listed ${r.objects.toLocaleString("en")} objects.`,
        progress: { objects: r.objects, bytes: r.bytes, batches: 15, elapsed_s: 41.2 },
        result: r,
        finished_at: iso(Date.now()),
      };
    } else {
      const share = x.polls / 3;
      x.scan = {
        ...x.scan,
        state: "running",
        progress: {
          objects: Math.round(r.objects * share),
          bytes: Math.round(r.bytes * share),
          batches: x.polls * 5,
          elapsed_s: x.polls * 13.7,
        },
      };
    }
  }
  save(s);
  return x.scan;
}

// --- Storage Mover --------------------------------------------------------------------------

function moverView(s: CloudState): S["StorageMoverSettings"] {
  const grant = s.grants.find((g) => g.state === "active" && g.purpose === "storage_mover");
  const can = grant
    ? {
        ok: true,
        reason: null,
        subscription_id: grant.scope.split("/")[2] ?? null,
        resource_group: rgOf(grant.scope),
      }
    : {
        ok: false,
        reason:
          "Connect Azure first, with a resource group for Storage Mover: the worker needs its Mover Deployer grant there.",
        subscription_id: null,
        resource_group: null,
      };
  const t = s.mover ? s.tasks[s.mover.taskId] : undefined;
  const p = t?.params ?? {};
  const deployment: S["MoverDeployment"] | null = t
    ? {
        task_id: t.task.task_id,
        state: t.task.state,
        detail: t.task.detail,
        subscription_id: String(p.subscription_id),
        resource_group: String(p.resource_group),
        name: String(p.name),
        project: String(p.project),
        location: String(p.location),
        created_at: t.task.created_at,
        finished_at: t.task.finished_at,
        config_value: moverConfig(p),
        registration: {
          tenant_id: TENANT,
          subscription_id: String(p.subscription_id),
          resource_group: String(p.resource_group),
          storage_mover: String(p.name),
        },
      }
    : null;
  const base: S["StorageMoverSettings"] = {
    configured: true,
    deployment,
    can_deploy: can,
    agent_steps: AGENT_STEPS,
    storage_mover: {
      name: "omnifile-mover",
      resource_group: "rg-omnifile",
      subscription_id: "11111111-2222-4333-8444-555566667777",
      provisioning_state: "Succeeded",
    },
    agents: [],
    projects: [
      {
        name: "migrations",
        description: "OmniFile migrations",
        provisioning_state: "Succeeded",
        in_use: true,
      },
    ],
    endpoints: {
      source: { "seismic-raw": "s3-seismic-raw" },
      target: { "omnilive01/data": "blob-omnilive01-data" },
    },
    error: null,
  };
  if (!deployment || deployment.state !== "done" || !s.mover) return base;
  // A deployment the worker finished: the page reads the new mover, and its agent
  // registers over the next reads (Offline, then Online).
  s.mover.agentReads += 1;
  const reads = s.mover.agentReads;
  save(s);
  return {
    ...base,
    storage_mover: {
      name: deployment.name,
      resource_group: deployment.resource_group,
      subscription_id: deployment.subscription_id,
      provisioning_state: "Succeeded",
    },
    projects: [
      {
        name: deployment.project,
        description: null,
        provisioning_state: "Succeeded",
        in_use: true,
      },
    ],
    agents:
      reads >= 2
        ? [
            {
              name: "smagent-dc1-01",
              status: reads >= 3 ? "Online" : "Offline",
              last_status_update: iso(Date.now() - 20_000),
              arc_resource_id: `/subscriptions/${deployment.subscription_id}/resourceGroups/${deployment.resource_group}/providers/Microsoft.HybridCompute/machines/smagent-dc1-01`,
              version: "3.1.2205",
            },
          ]
        : [],
    endpoints: { source: {}, target: {} },
  };
}

// --- seeds, for tests and screenshots -------------------------------------------------------

/** The cloud mock's state for a mock database (tests change it directly). */
export function cloudState(db: MockDb): CloudState {
  return load(db);
}

/**
 * Azure listed, chosen (rg-storage and rg-network, Storage Mover in rg-migration) and
 * every planned grant made, as two sign-ins would leave it.
 */
export function seedAzureConnected(db: MockDb, options: { discovered?: boolean } = {}): void {
  const s = load(db);
  const by = db.users.admin.display_name ?? null;
  s.listing = listing(by);
  s.choice = {
    subscription_id: DEMO_SUBSCRIPTION,
    resource_groups: ["rg-storage", "rg-network"],
    mover_resource_group: "rg-migration",
    chosen_at: iso(Date.now() - 60_000),
    chosen_by: by,
  };
  for (const i of plan(s)) {
    s.grants.push({
      grant_id: String(++s.nextId),
      purpose: i.purpose,
      role_name: i.role_name,
      scope: i.scope,
      principal: i.principal,
      principal_id: i.principal_id ?? "",
      assignment_id: `${i.scope}/providers/Microsoft.Authorization/roleAssignments/${guid(i.key)}`,
      state: "active",
      made_at: iso(Date.now() - 30_000),
      made_by: by,
      removed_at: null,
      removed_by: null,
    });
  }
  if (options.discovered) s.discovered = discovery(s);
  save(s);
}

// --- handlers -------------------------------------------------------------------------------

const REGION = /^[a-z]{2}(-[a-z]+)+-[0-9]$/;
const DEPLOYMENT = /^[a-z0-9][a-z0-9-]{1,19}$/;
const ACCOUNT = /^[0-9]{12}$/;
const BUCKET = /^[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]$/;
const MOVER_NAME = /^[A-Za-z0-9][A-Za-z0-9_-]{2,62}$/;
const LOCATION = /^[a-z0-9]{3,30}$/;

export function cloudHandlers(getDb: () => MockDb) {
  const admin = () => getDb().users[getDb().persona].roles.includes("ArchiveAdmin");
  const refuse = () => problem(403, "role_required", "needs the app role ArchiveAdmin");
  const st = () => load(getDb());
  const myName = () => {
    const db = getDb();
    const persona: Persona = db.persona;
    return db.users[persona].display_name ?? null;
  };

  return [
    // Connect Azure
    http.get("*/v1/settings/azure", () => {
      if (!admin()) return refuse();
      return json(azureView(st()));
    }),
    http.post("*/v1/settings/azure/sign-in", async ({ request }) => {
      if (!admin()) return refuse();
      const s = st();
      const b = (await request.json()) as { purpose?: string; grant_ids?: string[] };
      const now = Date.now();
      const opId = String(++s.nextId);
      let detail: string;
      if (b.purpose === "list") {
        s.listing = listing(myName());
        detail = "Listed 2 tenants, 1 subscription and 3 resource groups.";
      } else if (b.purpose === "grant") {
        const items = plan(s);
        if (!items.length) return precondition("choose a subscription and resource groups first");
        let made = 0;
        for (const i of items) {
          if (i.state === "active") continue;
          made += 1;
          s.grants.push({
            grant_id: String(++s.nextId),
            purpose: i.purpose,
            role_name: i.role_name,
            scope: i.scope,
            principal: i.principal,
            principal_id: i.principal_id ?? "",
            assignment_id: `${i.scope}/providers/Microsoft.Authorization/roleAssignments/${guid(i.key)}`,
            state: "active",
            made_at: iso(now),
            made_by: myName(),
            removed_at: null,
            removed_by: null,
          });
        }
        detail = `Made ${made} ${made === 1 ? "grant" : "grants"}: each a custom role and one role assignment.`;
      } else if (b.purpose === "remove") {
        const ids = b.grant_ids ?? [];
        if (!ids.length) return invalid("grant_ids", "one to 50 grant ids");
        const active = s.grants.filter((g) => g.state === "active");
        if (ids.some((id) => !active.some((g) => g.grant_id === id))) {
          return problem(404, "not_found", "no such active grant");
        }
        for (const g of s.grants) {
          if (ids.includes(g.grant_id)) {
            g.state = "removed";
            g.removed_at = iso(now);
            g.removed_by = myName();
          }
        }
        detail = `Removed ${ids.length} ${ids.length === 1 ? "grant" : "grants"}.`;
      } else {
        return invalid("purpose", "list, grant or remove");
      }
      s.lastOp = {
        op_id: opId,
        purpose: b.purpose,
        state: "done",
        detail,
        created_at: iso(now),
        finished_at: iso(now),
      };
      save(s);
      return json(
        {
          op_id: opId,
          purpose: b.purpose,
          authorize_url: `/settings/azure?op=${opId}`,
          expires_at: iso(now + 600_000),
        },
        { status: 201 },
      );
    }),
    http.put("*/v1/settings/azure/choice", async ({ request }) => {
      if (!admin()) return refuse();
      const s = st();
      const b = (await request.json()) as {
        subscription_id?: string;
        resource_groups?: string[];
        mover_resource_group?: string | null;
      };
      if (!s.listing) {
        return invalid(
          "subscription_id",
          "sign in to Azure first, so the service knows what you can see",
        );
      }
      if (!s.listing.subscriptions.some((x) => x.subscription_id === b.subscription_id)) {
        return invalid(
          "subscription_id",
          `subscription ${b.subscription_id ?? ""} isn't in your latest listing`,
        );
      }
      const known = new Set(s.listing.resource_groups.map((g) => g.name));
      const rgs = b.resource_groups ?? [];
      if (!rgs.length) return invalid("resource_groups", "choose at least one resource group");
      const unknown = [...rgs, b.mover_resource_group ?? ""].filter((r) => r && !known.has(r));
      if (unknown.length)
        return invalid("resource_groups", `${unknown.join(", ")} isn't in your listing`);
      s.choice = {
        subscription_id: b.subscription_id ?? DEMO_SUBSCRIPTION,
        resource_groups: [...new Set(rgs)],
        mover_resource_group: b.mover_resource_group || null,
        chosen_at: iso(Date.now()),
        chosen_by: myName(),
      };
      save(s);
      return json(azureView(s));
    }),
    http.post("*/v1/settings/azure/discover", () => {
      if (!admin()) return refuse();
      const s = st();
      if (!s.grants.some((g) => g.state === "active" && g.purpose === "discover")) {
        return precondition("grant the service its Discovery Reader roles first");
      }
      s.discovered = discovery(s);
      save(s);
      return json(azureView(s));
    }),
    http.post("*/v1/settings/azure/confirm", async ({ request }) => {
      if (!admin()) return refuse();
      const s = st();
      const d = s.discovered;
      if (!d) return precondition("discover first");
      const b = (await request.json()) as Partial<Record<keyof Confirmed, string[]>>;
      const accounts = b.storage_accounts ?? [];
      for (const id of accounts) {
        const a = d.storage_accounts.find((x) => x.id === id);
        if (!a) return invalid("storage_accounts", `${id} wasn't discovered`);
        if (!a.private) {
          return invalid(
            "storage_accounts",
            `${a.name} isn't reachable privately (decision 22): give it a blob private endpoint and turn public access off first`,
          );
        }
      }
      s.confirmed = {
        confirmed_at: iso(Date.now()),
        confirmed_by: myName(),
        storage_accounts: [...new Set(accounts)],
        virtual_networks: [...new Set(b.virtual_networks ?? [])],
        private_endpoints: [...new Set(b.private_endpoints ?? [])],
      };
      save(s);
      return json(azureView(s));
    }),

    // Connect AWS
    http.get("*/v1/settings/aws/connect/template", () => {
      if (!admin()) return refuse();
      return new HttpResponse(
        "AWSTemplateFormatVersion: '2010-09-09'\nDescription: OmniFile Gateway connect (mock)\n",
        { headers: { "Content-Type": "application/yaml" } },
      );
    }),
    http.get("*/v1/settings/aws/connect", ({ request }) => {
      if (!admin()) return refuse();
      const q = new URL(request.url).searchParams;
      const region = q.get("region") || "us-east-1";
      const dep = q.get("deployment_id") || "omnifile";
      if (!REGION.test(region)) return invalid("region", "an AWS region, e.g. us-east-1");
      if (!DEPLOYMENT.test(dep)) return invalid("deployment_id", "lowercase letters, digits and -");
      const params = {
        TenantId: TENANT,
        Audience: AUDIENCE,
        ServiceSubjects: SUBJECT,
        DeploymentId: dep,
        CreateOidcProvider: "true",
        SourceBuckets: "",
      };
      const console = `https://${region}.console.aws.amazon.com/cloudformation/home?region=${region}`;
      const stack = `omnifile-${dep}`;
      const templateUrl = st().templateUrl;
      const query = new URLSearchParams({ templateURL: templateUrl ?? "", stackName: stack });
      for (const [k, v] of Object.entries(params)) if (v) query.set(`param_${k}`, v);
      const first = templateUrl
        ? "Press Launch in AWS and sign in to the AWS account that holds the source buckets. OmniFile never sees your AWS sign-in."
        : "Download the template, open CloudFormation's Create stack page and choose Upload a template file. Type the parameter values shown here. (A one-click link needs the template in S3: OMNIFILE_AWS_CONNECT_TEMPLATE_URL.)";
      return json({
        ready: true,
        problems: [],
        tenant_id: TENANT,
        audience: AUDIENCE,
        subjects: [SUBJECT],
        private_network: true,
        region,
        deployment_id: dep,
        stack_name: stack,
        role_arn_pattern: `arn:aws:iam::<account>:role/omnifile-service-${dep}`,
        parameters: params,
        quick_create_url: templateUrl ? `${console}#/stacks/quickcreate?${query.toString()}` : null,
        console_create_url: `${console}#/stacks/create`,
        template_path: "/v1/settings/aws/connect/template",
        steps: [
          first,
          "Type the source bucket names into SourceBuckets (comma-separated). Leave the rest as filled in. Tick the IAM acknowledgement and create the stack.",
          "Private networking is on: give VpcId and SubnetIds so the stack makes the S3, STS and SQS interface endpoints, or use endpoints you already have. Fetch needs the S3 and STS endpoints from the stack's outputs.",
          "On each source bucket, turn on Properties, Amazon EventBridge. The stack can't, and OmniFile never changes a source bucket. AWS starts sending a bucket's events about five minutes after that; a change made before then sends none.",
          "When the stack shows CREATE_COMPLETE, come back and press Fetch with the account id and region. The service assumes the new role and reads the buckets.",
          "Confirm the account's settings. They go into the deployment's awsAccounts parameter (OMNIFILE_AWS_ACCOUNTS); then the account shows as configured here.",
        ],
        connections: st().connections,
      } satisfies S["AwsConnect"]);
    }),
    http.post("*/v1/settings/aws/connect/fetch", async ({ request }) => {
      if (!admin()) return refuse();
      const b = (await request.json()) as {
        account_id?: string;
        region?: string;
        deployment_id?: string;
        endpoints?: Record<string, string>;
      };
      if (!ACCOUNT.test(b.account_id ?? "")) return invalid("account_id", "12 digits");
      if (!REGION.test(b.region ?? "")) return invalid("region", "an AWS region");
      if (!DEPLOYMENT.test(b.deployment_id ?? "")) {
        return invalid("deployment_id", "lowercase letters, digits and -");
      }
      const eps = b.endpoints ?? {};
      for (const [k, v] of Object.entries(eps)) {
        if (!/^https:\/\/[a-z0-9.-]+\.vpce\.amazonaws\.com\/?$/.test(v)) {
          return invalid(
            `endpoints.${k}`,
            "https://<dns> of an interface endpoint (*.vpce.amazonaws.com)",
          );
        }
      }
      const s = st();
      const task = newTask(s, "aws_fetch", b);
      save(s);
      return json(task, { status: 202 });
    }),
    http.post("*/v1/settings/aws/connect/outputs", async ({ request }) => {
      if (!admin()) return refuse();
      const { text } = (await request.json()) as { text?: string };
      const outputs = parseOutputs(text ?? "");
      const account = outputs.AccountId ?? null;
      const region = outputs.Region ?? null;
      const dep = outputs.DeploymentId ?? null;
      const problems: string[] = [];
      if (!account || !ACCOUNT.test(account)) problems.push("No 12-digit AccountId.");
      if (!region || !REGION.test(region)) problems.push("No Region.");
      if (!dep || !DEPLOYMENT.test(dep)) problems.push("No DeploymentId.");
      const entry =
        account && region && dep && !problems.length
          ? entryOf(account, region, dep, outputs)
          : null;
      return json({
        account_id: account,
        region,
        deployment_id: dep,
        outputs,
        entry,
        buckets: (outputs.SourceBuckets ?? "").split(",").filter(Boolean),
        problems,
      } satisfies S["AwsConnectEntry"]);
    }),
    http.post("*/v1/settings/aws/connect/confirm", async ({ request }) => {
      if (!admin()) return refuse();
      const b = (await request.json()) as {
        account_id?: string;
        region?: string;
        deployment_id?: string;
        outputs?: Record<string, string> | null;
        buckets?: string[];
      };
      if (!ACCOUNT.test(b.account_id ?? "")) return invalid("account_id", "12 digits");
      if (!REGION.test(b.region ?? "")) return invalid("region", "an AWS region");
      if (!DEPLOYMENT.test(b.deployment_id ?? ""))
        return invalid("deployment_id", "a deployment id");
      if (!b.account_id || !b.region || !b.deployment_id) return invalid("account_id", "required");
      if (typeof b.outputs !== "object" || b.outputs === null) {
        return invalid("outputs", "the stack's outputs, as strings");
      }
      if (!Array.isArray(b.buckets) || b.buckets.some((x) => !BUCKET.test(x))) {
        return invalid("buckets", "S3 bucket names");
      }
      const s = st();
      const entry = entryOf(b.account_id, b.region, b.deployment_id, b.outputs);
      const conn: S["AwsConnection"] = {
        account_id: b.account_id,
        region: b.region,
        deployment_id: b.deployment_id,
        entry,
        buckets: [...new Set(b.buckets)].sort(),
        env_name: "OMNIFILE_AWS_ACCOUNTS",
        parameter_name: "awsAccounts",
        settings_json: settingsJson(s, b.account_id, entry),
        configured: b.account_id === KNOWN_ACCOUNT,
        confirmed_at: iso(Date.now()),
        confirmed_by: myName(),
        steps: [
          "Set the deployment's awsAccounts parameter (OMNIFILE_AWS_ACCOUNTS on the worker, api and web) to the value shown, and re-deploy (deploy/azure/README.md).",
          "Back here, press Test on the role: the worker assumes it with its web identity.",
          "Add each source bucket on the Storage page (an S3 backend), then start a migration.",
        ],
      };
      s.connections = [...s.connections.filter((c) => c.account_id !== conn.account_id), conn];
      save(s);
      return json(conn);
    }),

    // Storage Mover
    http.get("*/v1/settings/storage-mover", () => {
      if (!admin()) return refuse();
      return json(moverView(st()));
    }),
    http.post("*/v1/settings/storage-mover/deploy", async ({ request }) => {
      if (!admin()) return refuse();
      const b = (await request.json()) as { name?: string; project?: string; location?: string };
      if (!MOVER_NAME.test(b.name ?? ""))
        return invalid("name", "letters, digits, - and _ (3 to 63)");
      if (!MOVER_NAME.test(b.project ?? "")) {
        return invalid("project", "letters, digits, - and _ (3 to 63)");
      }
      if (!LOCATION.test(b.location ?? ""))
        return invalid("location", "an Azure region name, e.g. westeurope");
      const s = st();
      const can = moverView(s).can_deploy;
      if (!can.ok) return precondition(can.reason ?? "connect Azure first");
      const task = newTask(s, "mover_deploy", {
        subscription_id: can.subscription_id,
        resource_group: can.resource_group,
        name: b.name,
        project: b.project,
        location: b.location,
      });
      s.mover = { taskId: task.task_id, agentReads: 0 };
      save(s);
      return json(task, { status: 202 });
    }),
    http.get("*/v1/settings/tasks/:id", ({ params }) => {
      if (!admin()) return refuse();
      const t = advanceTask(st(), String(params.id));
      return t ? json(t) : problem(404, "not_found", "no such task");
    }),

    // Source scans
    http.post("*/v1/source-scans", async ({ request }) => {
      if (!admin()) return refuse();
      const b = (await request.json()) as {
        backend_id?: string;
        bucket?: string;
        prefix?: string;
        inventory?: { kind: string; bucket?: string; manifest_key?: string } | null;
        max_objects?: number;
        max_seconds?: number;
      };
      const backend = getDb().migration.sourceBackends.find((x) => x.backend_id === b.backend_id);
      if (!backend) return problem(404, "not_found", "no such backend");
      if (!BUCKET.test(b.bucket ?? "")) return invalid("bucket", "an S3 bucket name");
      if (backend.buckets && !backend.buckets.includes(b.bucket ?? "")) {
        return invalid(
          "bucket",
          `backend ${backend.backend_id} isn't set up for bucket ${b.bucket ?? ""}`,
        );
      }
      const prefix = b.prefix ?? "";
      if (prefix && !prefix.endsWith("/"))
        return invalid("prefix", "empty, or folders each ending in '/'");
      const s = st();
      const scan: S["SourceScan"] = {
        scan_id: String(++s.nextId),
        state: "pending",
        detail: null,
        backend_id: backend.backend_id,
        bucket: b.bucket ?? "",
        prefix,
        reader: b.inventory?.kind === "s3_inventory" ? "s3_inventory" : "listing",
        caps: { max_objects: b.max_objects ?? 2_000_000, max_seconds: b.max_seconds ?? 300 },
        progress: { objects: 0, bytes: 0, batches: 0, elapsed_s: 0 },
        result: null,
        created_at: iso(Date.now()),
        finished_at: null,
      };
      s.scans[scan.scan_id] = { scan, polls: 0 };
      save(s);
      return json(scan, { status: 202 });
    }),
    http.get("*/v1/source-scans/:id", ({ params }) => {
      if (!admin()) return refuse();
      const scan = advanceScan(st(), String(params.id));
      return scan ? json(scan) : problem(404, "not_found", "no such scan");
    }),
  ];
}
