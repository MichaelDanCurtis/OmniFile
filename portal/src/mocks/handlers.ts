import { http, HttpResponse, type JsonBodyType } from "msw";
import { example } from "./examples.gen";
import type {
  AsOfEntry,
  Check,
  Entry,
  FileDetail,
  Hold,
  Me,
  NewDataset,
  NewShare,
  PathSegment,
  Pin,
  PinPreview,
  PolicyBody,
  PolicyVersion,
  Proposal,
  ProblemCode,
  Recall,
  RecallOptions,
  RecallState,
  RecallStep,
  Share,
  ShareOverride,
  ShareSettingsPatch,
  VersionRestore,
} from "../api/types";
import { makeAnnexHandlers, resetAnnexMock } from "./annexHandlers";
import { createDb, type MockDb, type MockFile, type MockRecall, type Persona } from "./db";
import { consoleHandlers } from "./console";
import { migrationHandlers } from "./migrations";
import { mockPolicyPreview } from "./preview";
import { visualHandlers } from "./visual";
import { cloudHandlers } from "./cloud";
import { settingsHandlers } from "./settings";
import { pricesVisible, scrubMoney, wp18Handlers } from "./wp18";
import { restoredName, restoredTimestampedName, type RestoreTarget } from "../lib/restoreNames";

/**
 * MSW handlers for every route in portal-api.yaml (section 10 of portal-api.md).
 * They also enforce the client-side rules the SPA must follow: X-CSRF-Token on
 * unsafe calls, Idempotency-Key on POSTs (except previews), If-Match where the
 * contract needs it. Tests read `requestLog` to check them.
 */

let db: MockDb = createDb();
export const requestLog: {
  method: string;
  path: string;
  headers: Record<string, string>;
  body: unknown;
}[] = [];

export function resetDb(options: { persona?: Persona; now?: number } = {}): MockDb {
  db = createDb(options.now ?? Date.now(), options.persona ?? "admin");
  requestLog.length = 0;
  revokedEdges.clear();
  addedEdges.clear();
  resetAnnexMock(options.now);
  return db;
}

export function getDb(): MockDb {
  return db;
}

export function setPersona(persona: Persona): void {
  db.persona = persona;
}

const DAY = 86_400_000;
const NO_PROMISE_BYTES = 10_000_000_000;
const iso = (ms: number) => new Date(ms).toISOString();
const now = () => Date.now();
const csrfFor = (persona: Persona) => `mock-csrf-${persona}`;
const newId = () => String(++db.nextId);

function problem(
  status: number,
  code: ProblemCode,
  detail: string,
  extra: Record<string, unknown> = {},
) {
  return HttpResponse.json(
    {
      type: `urn:omnifile:error:${code}`,
      title: code.replace(/_/g, " "),
      status,
      code,
      detail,
      retryable: code === "groups_pending" || code === "throttled",
      retry_after_s: null,
      request_id: `mock-${Math.random().toString(16).slice(2, 10)}`,
      ...extra,
    } as JsonBodyType,
    { status, headers: { "Content-Type": "application/problem+json" } },
  );
}

/** Every answer, with its amounts removed when prices are hidden from the persona (5.24). */
const json = (body: unknown, init?: ResponseInit) =>
  HttpResponse.json((pricesVisible(db) ? body : scrubMoney(body)) as JsonBodyType, init);

const PREVIEW = /\/(policies\/preview|pins\/preview|settings\/preview)$/;

async function guard(request: Request): Promise<Response | null> {
  const url = new URL(request.url);
  let body: unknown = null;
  if (request.method !== "GET" && request.method !== "HEAD") {
    const text = await request.clone().text();
    body = text ? (JSON.parse(text) as unknown) : null;
  }
  requestLog.push({
    method: request.method,
    path: url.pathname + url.search,
    headers: Object.fromEntries(request.headers.entries()),
    body,
  });
  if (["POST", "PUT", "PATCH", "DELETE"].includes(request.method)) {
    if (request.headers.get("X-CSRF-Token") !== csrfFor(db.persona)) {
      return problem(403, "csrf_failed", "X-CSRF-Token is missing or wrong.");
    }
    if (
      request.method === "POST" &&
      url.pathname.startsWith("/v1/") &&
      !PREVIEW.test(url.pathname)
    ) {
      const key = request.headers.get("Idempotency-Key") ?? "";
      if (key.length < 16 || key.length > 128) {
        return problem(400, "invalid_request", "Idempotency-Key is required (16-128 characters).");
      }
    }
  }
  return null;
}

function me(): Me & { identity_id: string } {
  return { ...db.users[db.persona], prices_visible: pricesVisible(db) };
}

const isAdmin = () => me().roles.includes("ArchiveAdmin");
const isSteward = () => me().roles.includes("DataSteward");

function myAccess(share: Share): Share["my_access"] {
  const user = me();
  const owner = user.owner_of.includes(share.share_id);
  const writer = user.writer_of.includes(share.share_id);
  const reader = user.reader_of.includes(share.share_id);
  return { owner, writer, reader, can_read_root: owner || writer || reader };
}

function viewShare(share: Share): Share {
  const access = myAccess(share);
  return { ...share, my_access: access, nfs_clients: isAdmin() ? share.nfs_clients : [] };
}

const canReadShare = (shareId: string) => myAccess(shareById(shareId) as Share).can_read_root;
const shareById = (id: string) => db.shares.find((s) => s.share_id === id);
const isOwner = (shareId: string) => me().owner_of.includes(shareId);

function readableFile(id: string): MockFile | null {
  const file = db.files.get(id);
  if (!file || !canReadShare(file.share_id)) return null;
  return file;
}

function ancestors(file: MockFile): MockFile[] {
  const chain: MockFile[] = [];
  let cur = file.parent_id ? db.files.get(file.parent_id) : undefined;
  while (cur) {
    chain.unshift(cur);
    cur = cur.parent_id ? db.files.get(cur.parent_id) : undefined;
  }
  return chain;
}

function pathOf(file: MockFile): PathSegment[] {
  return [...ancestors(file), file].map((f) => ({ file_id: f.file_id, name: f.name }));
}

function activePin(file: MockFile): Entry["pin"] {
  const chain = [file, ...ancestors(file).reverse()];
  for (const f of chain) {
    const pin = db.pins.find(
      (p) =>
        p.file_id === f.file_id &&
        !p.released_at &&
        (!p.until || new Date(p.until).getTime() > now()),
    );
    if (pin) {
      return {
        pin_id: pin.pin_id,
        tier: pin.tier,
        until: pin.until ?? null,
        override_archive: pin.override_archive ?? false,
        inherited_from: f.file_id === file.file_id ? null : f.file_id,
      };
    }
  }
  return null;
}

function activeHolds(file: MockFile): Hold[] {
  const ids = new Set([file.file_id, ...ancestors(file).map((a) => a.file_id)]);
  return db.holds.filter(
    (h) =>
      ids.has(h.file_id) && !h.released_at && (!h.until || new Date(h.until).getTime() > now()),
  );
}

function activeRecallFor(file: MockFile): MockRecall | undefined {
  return db.recalls.find(
    (r) =>
      r.recall.file_id === file.file_id &&
      !["available", "failed", "cancelled", "superseded"].includes(advance(r).recall.state),
  );
}

function toEntry(file: MockFile): Entry {
  const holds = activeHolds(file);
  const keep = holds.filter((h) => h.kind === "keep_online");
  const recall = activeRecallFor(file);
  const keepUntil = keep.some((h) => !h.until)
    ? null
    : (keep
        .map((h) => h.until ?? "")
        .sort()
        .at(-1) ?? null);
  return {
    file_id: file.file_id,
    name: file.name,
    kind: file.kind,
    symlink_target: null,
    size: file.size,
    mtime: file.versions[0]?.committed_at ?? file.created_at,
    mtime_ns: file.versions[0]?.mtime_ns ?? null,
    state: file.state,
    resident_bytes: file.state === "resident" ? file.size : Math.min(file.size, 4_000_000),
    version_id: file.versions[0]?.version_id ?? null,
    tier: file.tier,
    archive_at: file.archive_at,
    next_action: file.next_action,
    next_action_at: file.next_action_at,
    pin: activePin(file),
    holds: {
      keep_online: keep.length > 0,
      keep_online_until: keep.length > 0 ? keepUntil : null,
      legal: holds.some((h) => h.kind === "legal") || file.legal_hold,
      items: holdItems(file),
    },
    active_recall: recall
      ? {
          recall_id: recall.recall.recall_id,
          state: recall.recall.state,
          eta: recall.recall.eta ?? null,
        }
      : null,
  };
}

function toDetail(file: MockFile): FileDetail {
  const share = shareById(file.share_id) as Share;
  const share_ = share;
  return {
    ...toEntry(file),
    share_id: file.share_id,
    dataset_id: share_.dataset_id,
    path: pathOf(file),
    blake3:
      file.kind === "file"
        ? "a3f1c9e07b2d4e6f8a1b3c5d7e9f0a2b4c6d8e0f1a3b5c7d9e1f2a4b6c8d0e2f"
        : null,
    crc64nvme: file.kind === "file" ? "9c4e1f0a7b2d3e58" : null,
    owner_sid: "S-1-5-21-3623811015-3361044348-30300820-4102",
    uid: 41102,
    gid: 51201,
    mode: "0640",
    written_at: file.versions[0]?.committed_at ?? file.created_at,
    last_read_at: file.last_read_at,
    retention_until: file.retention_until,
    legal_hold: file.legal_hold || activeHolds(file).some((h) => h.kind === "legal"),
    versions_count: file.versions.length,
    rewind_depth_days: share.rewind.depth_days,
  };
}

const STEP_OF: Record<RecallState, number> = {
  requested: 0,
  rehydrating: 1,
  copying: 1,
  downloading: 2,
  available: 3,
  failed: -1,
  cancelled: -1,
  superseded: -1,
};

/** Simulated recalls move on with time, so the four-step view animates in dev. */
function advance(r: MockRecall): MockRecall {
  if (!r.simulate) return r;
  const elapsed = now() - new Date(r.recall.requested_at).getTime();
  const plan: [RecallState, number][] = [
    ["rehydrating", 4_000],
    ["downloading", 12_000],
    ["available", 20_000],
  ];
  for (const [state, after] of plan) {
    if (elapsed >= after && !r.log.some((l) => l.state === state)) {
      const at = iso(new Date(r.recall.requested_at).getTime() + after);
      r.log.push({ state, at });
      r.recall.state = state;
      if (state === "available") r.recall.completed_at = at;
    }
  }
  return r;
}

function toRecall(r: MockRecall): Recall {
  advance(r);
  const state = r.recall.state;
  const order: RecallStep["step"][] = ["requested", "rehydrating", "downloading", "available"];
  const reached =
    state === "failed" || state === "superseded"
      ? Math.max(...r.log.map((l) => STEP_OF[l.state]))
      : STEP_OF[state];
  const steps: RecallStep[] = order.map((step, i) => {
    const at = r.log.find((l) => STEP_OF[l.state] === i && l.state !== "failed")?.at ?? null;
    let status: RecallStep["status"] = "pending";
    if (state === "failed") {
      status =
        i <= reached
          ? i === reached && i < 3
            ? "done"
            : "done"
          : i === reached + 1
            ? "failed"
            : "pending";
    } else if (state === "superseded") {
      // Stopped, not failed: what it reached is done, the rest never happened.
      status = i <= reached ? "done" : "pending";
    } else if (i < reached || (i === 3 && state === "available")) status = "done";
    else if (i === reached) status = "current";
    return {
      step,
      status,
      at: status === "failed" ? (r.log.find((l) => l.state === "failed")?.at ?? null) : at,
    };
  });
  const { requesters_count, ...rest } = r.recall;
  const file = readableFile(r.recall.file_id);
  return {
    ...rest,
    parent_id: file?.parent_id ?? null,
    steps,
    requesters: { count: requesters_count, me: r.requested_by === db.persona },
  };
}

function recallOptions(file: MockFile, versionId?: string): RecallOptions {
  const needs = file.tier.archived && file.tier.restore_state !== "completed";
  const small = file.size <= NO_PROMISE_BYTES;
  const recall = activeRecallFor(file);
  const gb = file.size / 1e9;
  return {
    file_id: file.file_id,
    version_id: versionId ?? file.versions[0]?.version_id ?? "0",
    size: file.size,
    needs_restore: needs,
    online: !needs,
    no_promise_above_bytes: NO_PROMISE_BYTES,
    active_recall: recall
      ? {
          recall_id: recall.recall.recall_id,
          state: recall.recall.state,
          eta: recall.recall.eta ?? null,
        }
      : null,
    options: [
      {
        priority: "standard",
        offered: true,
        reason: null,
        eta: small ? iso(now() + 15 * 3600_000) : null,
        eta_confidence: small ? "provider_sla" : "none",
        fee_estimate: { amount: (gb * 0.02).toFixed(2), currency: "USD" },
      },
      small
        ? {
            priority: "high",
            offered: true,
            reason: null,
            eta: iso(now() + 3600_000),
            eta_confidence: "provider_sla",
            fee_estimate: { amount: (gb * 0.1).toFixed(2), currency: "USD" },
          }
        : {
            priority: "high",
            offered: false,
            reason:
              "High priority wouldn't be faster for a file this size: Azure restores at most 10 GiB per hour per account at High priority.",
            eta: null,
            eta_confidence: "none",
            fee_estimate: null,
          },
    ],
  };
}

function page<T>(items: T[], url: URL, max = 500) {
  const limit = Math.min(Number(url.searchParams.get("limit") ?? 100), max);
  const start = Number(url.searchParams.get("cursor") ?? 0);
  const slice = items.slice(start, start + limit);
  return { items: slice, next_cursor: start + limit < items.length ? String(start + limit) : null };
}

function etagOk(request: Request, etag: string): Response | null {
  const ifMatch = request.headers.get("If-Match");
  if (!ifMatch) return problem(428, "precondition_required", "If-Match is required.");
  if (ifMatch !== etag) {
    return problem(412, "precondition_failed", "The share changed since you loaded it.");
  }
  return null;
}

function bumpEtag(share: Share) {
  share.etag = `"${Number(share.etag.replace(/"/g, "")) + 1}"`;
}

function needsReauth(): Response | null {
  if (now() - db.authTime > 600_000) {
    return problem(401, "reauth_required", "Sign in again to do this.", { max_age_s: 600 });
  }
  return null;
}

function audit(action: string, extra: Record<string, unknown> = {}) {
  const user = me();
  db.audit.unshift({
    audit_id: newId(),
    at: iso(now()),
    actor: `user:${user.identity_id}`,
    actor_display_name: user.display_name ?? null,
    actor_sid: user.sid ?? null,
    proof: "proven",
    trust_level: "proven",
    action,
    decision: "allow",
    dataset_id: null,
    share_id: null,
    file_id: null,
    version_id: null,
    client_addr: "10.40.2.17",
    details: {},
    ...extra,
  });
}

function personRef() {
  return {
    kind: "identity" as const,
    display_name: me().display_name ?? null,
    app_id: null,
    identity_id: me().identity_id,
  };
}

/** The holds that apply to a file: on it or on a folder above it. */
function holdItems(file: MockFile) {
  return activeHolds(file).map((h) => ({
    hold_id: h.hold_id,
    kind: h.kind,
    until: h.until ?? null,
    inherited_from: h.file_id === file.file_id ? null : h.file_id,
    set_by_identity_id: h.set_by?.identity_id ?? null,
  }));
}

function activePolicy(datasetId: string): PolicyVersion | undefined {
  return db.policies.find((p) => p.dataset_id === datasetId && p.state === "active");
}

function diffBodies(active: PolicyBody | undefined, proposed: PolicyBody) {
  const flat = (b: PolicyBody | undefined): Record<string, unknown> => {
    if (!b) return {};
    const { notify, ...rest } = b;
    const out: Record<string, unknown> = { ...rest };
    for (const [k, v] of Object.entries(notify ?? {})) out[`notify.${k}`] = v;
    return out;
  };
  const a = flat(active);
  const p = flat(proposed);
  const keys = [...new Set([...Object.keys(a), ...Object.keys(p)])];
  return keys
    .filter((k) => JSON.stringify(a[k] ?? null) !== JSON.stringify(p[k] ?? null))
    .map((field) => ({ field, active: a[field] ?? null, proposed: p[field] ?? null }));
}

function proposalView(entry: { proposal: Proposal }, detail: boolean): Proposal {
  const p = entry.proposal;
  const active = activePolicy(p.dataset_id);
  const stale = p.state === "pending" && (active?.version ?? null) !== (p.base_version ?? null);
  if (!detail) return { ...p, stale };
  const dataset = db.datasets.find((d) => d.dataset_id === p.dataset_id);
  const shares = db.shares.filter((s) => s.dataset_id === p.dataset_id);
  return {
    ...p,
    stale,
    preview: dataset
      ? mockPolicyPreview(p.body, dataset, shares, p.share_overrides ?? [])
      : undefined,
    diff: diffBodies(active?.body, p.body),
  };
}

function createPolicyVersion(
  datasetId: string,
  body: PolicyBody,
  note: string | null,
  proposalId: string | null,
) {
  const old = activePolicy(datasetId);
  if (old) old.state = "superseded";
  const policy: PolicyVersion = {
    policy_id: newId(),
    dataset_id: datasetId,
    version: (old?.version ?? 0) + 1,
    body,
    state: "active",
    proposal_id: proposalId,
    approved_by: personRef(),
    approved_at: iso(now()),
    note,
  };
  db.policies.unshift(policy);
  const dataset = db.datasets.find((d) => d.dataset_id === datasetId);
  if (dataset) {
    dataset.active_policy = {
      version: policy.version,
      mode: body.archive_day === undefined ? "m1" : (body.mode ?? "scheduled"),
      archive_day: body.archive_day ?? null,
    };
  }
  audit("policy.create", { dataset_id: datasetId, details: { version: policy.version } });
  return policy;
}

function pinChecks(
  file: MockFile,
  tier: Pin["tier"],
  overrideArchive: boolean,
): { checks: Check[]; fee: number; monthly: number } {
  const checks: Check[] = [];
  const order = ["hot", "warm", "cold", "archive"];
  const current = file.tier.logical ?? "hot";
  const price: Record<string, number> = {
    hot: 0.0184,
    warm: 0.01,
    cold: 0.0036,
    archive: 0.00099,
    smart: 0.0184,
  };
  const gb = file.size / 1e9;
  const monthly = ((price[tier] ?? 0) - (price[current] ?? 0)) * gb;
  let fee = 0;
  const changed = file.tier.tier_changed_at
    ? (now() - new Date(file.tier.tier_changed_at).getTime()) / DAY
    : 999;
  const minDays: Record<string, number> = { warm: 30, cold: 90, archive: 180 };
  const min = minDays[current] ?? 0;
  if (tier !== current && changed < min) {
    fee = ((min - changed) / 30.4) * (price[current] ?? 0) * gb;
    checks.push({
      severity: "warning",
      code: "pin_early_deletion",
      message: `${file.name} has been in its tier for ${Math.floor(changed)} of the ${min} days Azure bills at least. Moving it now costs an early-deletion fee.`,
      field: null,
      share_id: file.share_id,
      fee: { amount: fee.toFixed(2), currency: "USD" },
    });
  }
  if (
    file.tier.logical === "smart" ||
    (current === "hot" &&
      tier !== "hot" &&
      !file.tier.explicit_tier_set &&
      file.tier.native_class === "Smart")
  ) {
    checks.push({
      severity: "warning",
      code: "pin_leaves_smart",
      message: `${file.name} leaves Smart tier for good: Azure can't put a blob back into Smart once it has an explicit tier.`,
      field: null,
      share_id: file.share_id,
      fee: null,
    });
  }
  if (file.tier.archived && order.indexOf(tier) < order.indexOf("archive")) {
    checks.push({
      severity: "warning",
      code: "pin_recall_time",
      message:
        file.size > NO_PROMISE_BYTES
          ? `${file.name} is archived and over 10 GB: the pin starts a recall, and no time is promised.`
          : `${file.name} is archived: the pin starts a recall that takes up to 15 hours.`,
      field: null,
      share_id: file.share_id,
      fee: null,
    });
  }
  if (file.archive_at && order.indexOf(tier) < order.indexOf("archive") && !overrideArchive) {
    checks.push({
      severity: "info",
      code: "pin_past_archive_date",
      message: `The pin ends on ${file.name}'s archive date (${file.archive_at}) unless an Owner overrides it.`,
      field: null,
      share_id: file.share_id,
      fee: null,
    });
  }
  return { checks, fee, monthly };
}

function expand(ids: string[]): MockFile[] {
  const out: MockFile[] = [];
  const visit = (f: MockFile) => {
    if (f.deleted_at) return;
    if (f.kind === "file") out.push(f);
    else for (const child of db.files.values()) if (child.parent_id === f.file_id) visit(child);
  };
  for (const id of ids) {
    const f = readableFile(id);
    if (f) visit(f);
  }
  return out;
}

/** The rules POST /v1/shares checks (routes_provision.py); the first one broken, as a 400. */
function newShareProblem(b: Partial<NewShare>): Response | null {
  const bad = (field: string, message: string) =>
    problem(400, "invalid_request", `${field}: ${message}`, { errors: [{ field, message }] });
  if (
    !/^[A-Za-z0-9][A-Za-z0-9._-]{0,79}$/.test(b.name ?? "") ||
    ["global", "homes", "printers", "ipc$"].includes((b.name ?? "").toLowerCase())
  ) {
    return bad("name", "1 to 80 letters, digits, '.', '_' or '-'; not global, homes or printers");
  }
  const protocols = b.protocols ?? [];
  if (protocols.length === 0) return bad("protocols", "at least one of nfs3, nfs4, smb");
  const nfs = protocols.some((p) => p !== "smb");
  const sec = b.nfs_sec ?? [];
  const clients = b.nfs_clients ?? [];
  if (nfs) {
    if (sec.length === 0) return bad("nfs_sec", "an NFS share needs a security flavour");
    if (sec.includes("sys")) {
      if (clients.length === 0)
        return bad("nfs_clients", "sec=sys trusts the client: list the hosts or subnets");
      const wild = clients.find((c) => /[*?[]/.test(c) || c.endsWith("/0"));
      if (wild) return bad("nfs_clients", `'${wild}' lets in any host; name the hosts or subnets`);
      if (b.root_squash === false) return bad("root_squash", "sec=sys shares squash root");
    }
  } else if (sec.length || clients.length) {
    return bad(sec.length ? "nfs_sec" : "nfs_clients", "only for NFS shares");
  }
  if (!/^S-1-5-21(-\d+){4}$/.test(b.owners_group_sid ?? "")) {
    return bad("owners_group_sid", "an AD group's SID is required");
  }
  const onOpen = b.on_open ?? "fail";
  if (onOpen === "auto" && !(Number(b.wait_threshold_minutes) > 0)) {
    return bad("wait_threshold_minutes", "minutes, with on_open auto");
  }
  if (onOpen !== "auto" && b.wait_threshold_minutes !== undefined) {
    return bad("wait_threshold_minutes", "only with on_open auto");
  }
  if ((b.rewind_depth_days ?? 0) > (b.rewind_depth_max_days ?? 0)) {
    return bad("rewind_depth_days", "at most rewind_depth_max_days");
  }
  return null;
}

/** The folder at `path` (`/a/b`) under a dataset's root folder, or null. */
function folderOf(rootDirId: string | undefined, path: string): string | null {
  let dirId = rootDirId ?? null;
  for (const name of path.split("/").filter(Boolean)) {
    if (!dirId) return null;
    const parent: string = dirId;
    const next = [...db.files.values()].find(
      (f) => f.parent_id === parent && f.kind === "dir" && !f.deleted_at && f.name === name,
    );
    dirId = next?.file_id ?? null;
  }
  return dirId;
}

/** The checks a new share comes back with (the Kerberos note among them). */
function newShareChecks(shareId: string, sec: Share["nfs_sec"]): Check[] {
  const checks: Check[] = [];
  const krb = sec.filter((s) => s !== "sys");
  if (krb.length) {
    checks.push({
      severity: "info",
      code: "kerberos_throughput",
      message: `This share uses Kerberos (${krb.join(", ")}). Microsoft measured about 53% fewer IOPS with krb5, and about 77% fewer with krb5p, than with sec=sys on NFS.`,
      field: "nfs_sec",
      share_id: shareId,
      fee: null,
    });
  }
  if (sec.includes("sys")) {
    checks.push({
      severity: "warning",
      code: "host_trusted",
      message:
        "sec=sys: the NFS client says who the user is, so access is only as strong as the listed hosts.",
      field: "nfs_sec",
      share_id: shareId,
      fee: null,
    });
  }
  return checks;
}

/** Edges revoked in this session of the mock. */
const revokedEdges = new Set<string>();
/** Edges added in this session (Add edge): pending, then enrolled after a few polls. */
const addedEdges = new Map<string, { name: string; created: number; polls: number }>();
const ENROL_AFTER = import.meta.env.MODE === "test" ? { polls: 2, ms: 0 } : { polls: 2, ms: 9000 };

/** Dev mock only (screenshots): `omnifile.mockNoEdges` in localStorage makes every enrolled edge silent. */
function noEdgesOnline(): boolean {
  if (import.meta.env.MODE === "test") return false;
  try {
    return localStorage.getItem("omnifile.mockNoEdges") === "1";
  } catch {
    return false;
  }
}

/** The fixed edges, less the revoked. */
function fixedEdges() {
  const all = mockEdges().filter((e) => !revokedEdges.has(e.edge_id));
  if (!noEdgesOnline()) return all;
  const silent = new Date(now() - 42 * 60_000).toISOString();
  return all.map((e) =>
    e.state === "active" ? { ...e, status: "silent", last_heartbeat_at: silent } : e,
  );
}

/** Every edge the mock lists: the fixed ones and those added, less the revoked. */
function listedEdges() {
  const added = [...addedEdges].map(([id, a]) => {
    a.polls++;
    const enrolled = a.polls > ENROL_AFTER.polls && now() - a.created >= ENROL_AFTER.ms;
    const beat = enrolled && a.polls > ENROL_AFTER.polls + 1;
    return {
      edge_id: id,
      name: a.name,
      state: enrolled ? "active" : "pending",
      status: beat ? "ok" : enrolled ? "silent" : "pending",
      software_version: enrolled ? "0.3.0" : null,
      kernel: enrolled ? "7.0.0-14-generic" : null,
      guard_state: beat ? "armed" : null,
      last_heartbeat_at: beat ? new Date(now() - 2000).toISOString() : null,
      certificate_not_after: enrolled ? new Date(now() + 30 * DAY).toISOString() : null,
      health: beat ? { outbox_depth: 0, fills_running: 0, recalls_waiting: 0 } : {},
    };
  });
  return [...fixedEdges(), ...added].filter((e) => !revokedEdges.has(e.edge_id));
}

/** The edges GET /v1/edges reports; POST /v1/datasets checks against them. */
function mockEdges() {
  const ago = (m: number) => new Date(now() - m * 60_000).toISOString();
  const health = (free: number) => ({
    outbox_depth: 4,
    fills_running: 1,
    recalls_waiting: 2,
    data_free_bytes: free * 1024 ** 4,
    data_capacity_bytes: 20 * 1024 ** 4,
    fsal_connections: 1,
  });
  const edge = (id: string, name: string, extra: object) => ({
    edge_id: id,
    name,
    state: "active",
    status: "ok",
    software_version: "0.3.0",
    kernel: "7.0.0-14-generic",
    guard_state: "armed",
    last_heartbeat_at: ago(1),
    certificate_not_after: ago(-60 * 24 * 20),
    health: health(12),
    ...extra,
  });
  return [
    edge("1", "edge-houston-1", {}),
    edge("2", "edge-houston-2", {
      status: "attention",
      guard_state: "down",
      health: { ...health(10), recovery_used: 1 },
    }),
    edge("3", "edge-perth-1", {
      status: "attention",
      health: { ...health(1), quiet_stale_s: 400 },
    }),
    edge("4", "edge-aberdeen-1", { status: "silent", last_heartbeat_at: ago(42) }),
    edge("5", "edge-new", {
      state: "pending",
      status: "pending",
      software_version: null,
      kernel: null,
      guard_state: null,
      last_heartbeat_at: null,
      certificate_not_after: null,
      health: {},
    }),
  ];
}

export const handlers = [
  http.all("*", async ({ request }) => {
    const url = new URL(request.url);
    if (!/^\/(v1|auth)\//.test(url.pathname)) return undefined;
    const refused = await guard(request);
    if (refused) return refused;
    // ACL-checked routes wait for an over-200-groups user's groups (portal-api.md 3.3).
    if (me().groups_source === "pending" && /^\/v1\/(shares|dirs|files)(\/|$)/.test(url.pathname)) {
      return problem(503, "groups_pending", "Your AD groups are being resolved.", {
        retry_after_s: 5,
      });
    }
    return undefined;
  }),

  // WP18: the live feed, the appliance's images, who sees prices and the price editor.
  ...wp18Handlers({
    db: () => db,
    isAdmin: () => isAdmin(),
    isSteward: () => isSteward(),
    readableFile: (id) => readableFile(id),
    edges: () => fixedEdges(),
    problem,
  }),

  http.get("*/auth/session", () =>
    json({
      authenticated: true,
      user: me(),
      csrf_token: csrfFor(db.persona),
      expires_at: iso(now() + 10 * 3600_000),
      idle_expires_at: iso(now() + 3600_000),
      auth_time: iso(db.authTime),
    }),
  ),
  http.post("*/auth/logout", () => json({ logout_url: "/signed-out" })),
  http.get("*/v1/me", () => json(me())),

  http.get("*/v1/directory/groups", ({ request }) => {
    if (!isAdmin() && me().owner_of.length === 0)
      return problem(403, "role_required", "ArchiveAdmin or a share Owner.");
    const url = new URL(request.url);
    const q = (url.searchParams.get("q") ?? "").toLowerCase();
    const items = db.groups.filter(
      (g) =>
        g.sid.toLowerCase() === q ||
        (g.display_name ?? "").toLowerCase().startsWith(q) ||
        (g.sam_account_name ?? "").toLowerCase().startsWith(q) ||
        (g.display_name ?? "").toLowerCase().includes(q),
    );
    return json(page(items, url));
  }),

  http.get("*/v1/shares", ({ request }) => {
    const visible = db.shares.filter((s) => isAdmin() || isSteward() || myAccess(s).can_read_root);
    return json(page(visible.map(viewShare), new URL(request.url)));
  }),
  http.post("*/v1/shares", async ({ request }) => {
    if (!isAdmin()) return problem(403, "role_required", "ArchiveAdmin only.");
    const sent = (await request.json()) as Partial<NewShare>;
    const dataset = db.datasets.find((d) => d.dataset_id === sent.dataset_id);
    if (!dataset) {
      return problem(400, "invalid_request", "dataset_id: no such dataset", {
        errors: [{ field: "dataset_id", message: "no such dataset" }],
      });
    }
    const refused = newShareProblem(sent);
    if (refused) return refused;
    const b = sent as NewShare;
    const path = b.path ?? "/";
    const badPath = (message: string) =>
      problem(400, "invalid_request", `path: ${message}`, {
        errors: [{ field: "path", message }],
      });
    if (!/^\/(?:[^/\0]+(?:\/[^/\0]+)*)?$/.test(path) || /\/\.\.?(?:\/|$)/.test(path)) {
      return badPath("'/' or a folder such as /projects/2026");
    }
    const pathOf = (shareId: string) => db.sharePaths.get(shareId) ?? "/";
    let rootDirId = dataset.root_dir_id ?? newId();
    if (path !== "/") {
      if (!dataset.shares.some((s) => pathOf(s.share_id) === "/")) {
        return badPath("share the dataset's root first; a folder can be shared only after it");
      }
      const dir = folderOf(dataset.root_dir_id, path);
      if (!dir) return badPath(`no folder ${path} in the dataset`);
      rootDirId = dir;
    }
    const same = dataset.shares.find((s) => pathOf(s.share_id) === path);
    if (same) {
      return problem(
        409,
        "dataset_shared",
        path === "/"
          ? `the dataset's root is already shared as ${same.name}`
          : `${path} is already shared as ${same.name}`,
      );
    }
    // The mock has one edge namespace: any share by that name is taken.
    if (db.shares.some((s) => s.name.toLowerCase() === b.name.toLowerCase())) {
      return problem(409, "name_taken", `the edge already has a share named ${b.name}`);
    }
    const shareId = newId();
    const group = (sid: string | undefined) =>
      sid ? (db.groups.find((g) => g.sid === sid) ?? { sid }) : null;
    const sec = b.protocols.some((p) => p !== "smb") ? (b.nfs_sec ?? []) : [];
    const share: Share = {
      share_id: shareId,
      name: b.name,
      dataset_id: dataset.dataset_id,
      dataset_name: dataset.name,
      edge_id: dataset.edge_id ?? "1",
      root_dir_id: rootDirId,
      protocols: b.protocols,
      nfs_sec: sec,
      nfs_clients: sec.includes("sys") ? (b.nfs_clients ?? []) : [],
      root_squash: b.root_squash ?? true,
      trust_level: sec.includes("sys") ? "host_trusted" : "proven",
      host_trust_override: b.host_trust_override ?? false,
      ...(sec.length ? { nfs_export_id: 100 + db.shares.length } : {}),
      smb_encrypt: b.smb_encrypt ?? false,
      groups: {
        owners: group(b.owners_group_sid) ?? { sid: b.owners_group_sid },
        writers: group(b.writers_group_sid),
        readers: group(b.readers_group_sid),
      },
      on_open: b.on_open ?? "fail",
      wait_threshold_minutes: b.on_open === "auto" ? (b.wait_threshold_minutes ?? null) : null,
      quiet_s: null,
      quiet_default_s: 10,
      quiet_supported: true,
      edge_name: "edge-houston-1",
      rewind: { depth_days: b.rewind_depth_days ?? 0, max_days: b.rewind_depth_max_days ?? 0 },
      worm: { enabled: false, mode: null, policy_state: "none" },
      last_full_acl_scan_at: null,
      my_access: { owner: false, writer: false, reader: false, can_read_root: false },
      checks: newShareChecks(shareId, sec),
      etag: '"1"',
    };
    db.shares.push(share);
    if (path !== "/") db.sharePaths.set(shareId, path);
    dataset.shares.push({ share_id: shareId, name: b.name, worm_enabled: false });
    audit("share.add", { dataset_id: dataset.dataset_id, share_id: shareId });
    return json(viewShare(share), { status: 201, headers: { ETag: share.etag } });
  }),
  http.get("*/v1/shares/:id", ({ params }) => {
    const share = shareById(String(params.id));
    if (!share) return problem(404, "not_found", "No such share.");
    return json(viewShare(share), { headers: { ETag: share.etag } });
  }),
  http.delete("*/v1/shares/:id", ({ params, request }) => {
    if (!isAdmin()) return problem(403, "role_required", "ArchiveAdmin only.");
    const share = shareById(String(params.id));
    if (!share) return problem(404, "not_found", "No such share.");
    const stale = etagOk(request, share.etag);
    if (stale) return stale;
    if (share.worm.enabled) {
      return problem(409, "worm_violation", `${share.name} is a WORM share; it is never removed.`);
    }
    const dataset = db.datasets.find((d) => d.dataset_id === share.dataset_id);
    const pathOf = (shareId: string) => db.sharePaths.get(shareId) ?? "/";
    if (pathOf(share.share_id) === "/" && dataset) {
      const folders = dataset.shares.filter(
        (s) => s.share_id !== share.share_id && pathOf(s.share_id) !== "/",
      );
      if (folders.length > 0) {
        return problem(
          409,
          "share_in_use",
          `${share.name} serves the dataset's root, which ${folders.map((s) => s.name).join(", ")} still need${folders.length === 1 ? "s" : ""}. Remove ${folders.length === 1 ? "that share" : "those shares"} first.`,
        );
      }
    }
    db.shares = db.shares.filter((s) => s.share_id !== share.share_id);
    db.sharePaths.delete(share.share_id);
    if (dataset) dataset.shares = dataset.shares.filter((s) => s.share_id !== share.share_id);
    audit("share.remove", { dataset_id: share.dataset_id, share_id: share.share_id });
    return json({ share_id: share.share_id, removed: true });
  }),
  http.post("*/v1/shares/:id/settings/preview", async ({ params, request }) => {
    const share = shareById(String(params.id));
    if (!share) return problem(404, "not_found", "No such share.");
    const patch = (await request.json()) as ShareSettingsPatch;
    const sec = patch.nfs_sec ?? share.nfs_sec;
    const checks: Check[] = [];
    if (sec.includes("krb5") || sec.includes("krb5p") || sec.includes("krb5i")) {
      checks.push({
        severity: "info",
        code: "kerberos_throughput",
        message:
          "Kerberos costs throughput: Microsoft measured about 53% fewer IOPS with krb5 and about 77% fewer with krb5p than with sec=sys on NFS.",
        field: "nfs_sec",
        share_id: share.share_id,
        fee: null,
      });
    }
    if (sec.includes("sys")) {
      checks.push({
        severity: "warning",
        code: "host_trusted",
        message:
          "sec=sys trusts the uid the client sends. Name the client hosts and keep root_squash on.",
        field: "nfs_sec",
        share_id: share.share_id,
        fee: null,
      });
      if ((patch.nfs_clients ?? share.nfs_clients ?? []).length === 0) {
        checks.push({
          severity: "error",
          code: "invalid_body",
          message: "sec=sys needs at least one named client host.",
          field: "nfs_clients",
          share_id: share.share_id,
          fee: null,
        });
      }
    }
    return json({ checks });
  }),
  http.patch("*/v1/shares/:id/settings", async ({ params, request }) => {
    const share = shareById(String(params.id));
    if (!share) return problem(404, "not_found", "No such share.");
    const stale = etagOk(request, share.etag);
    if (stale) return stale;
    const patch = (await request.json()) as ShareSettingsPatch;
    const owner = isOwner(share.share_id);
    if (
      ("on_open" in patch ||
        "rewind_depth_days" in patch ||
        "wait_threshold_minutes" in patch ||
        "quiet_s" in patch) &&
      !owner &&
      !isAdmin()
    ) {
      return problem(
        403,
        "forbidden",
        "Only the share's Owners or an ArchiveAdmin can change this.",
      );
    }
    if (
      ("nfs_sec" in patch ||
        "rewind_depth_max_days" in patch ||
        "nfs_clients" in patch ||
        "root_squash" in patch ||
        "host_trust_override" in patch) &&
      !isAdmin()
    ) {
      return problem(403, "role_required", "ArchiveAdmin role required.");
    }
    if (
      patch.rewind_depth_days !== undefined &&
      patch.rewind_depth_days > (patch.rewind_depth_max_days ?? share.rewind.max_days)
    ) {
      return problem(400, "invalid_request", "Depth is over the maximum.", {
        errors: [{ field: "rewind_depth_days", message: `At most ${share.rewind.max_days} days.` }],
      });
    }
    if (patch.quiet_s != null && (patch.quiet_s < 10 || patch.quiet_s > 600)) {
      return problem(400, "invalid_request", "Out of range.", {
        errors: [
          { field: "quiet_s", message: "must be 10 to 600 seconds, or null for the default" },
        ],
      });
    }
    if (
      share.worm.enabled &&
      patch.quiet_s != null &&
      patch.quiet_s < 60 &&
      patch.quiet_s_confirmed !== true
    ) {
      return problem(400, "invalid_request", "Needs confirming.", {
        errors: [{ field: "quiet_s_confirmed", message: "below 60 s on a WORM share needs it" }],
      });
    }
    if ("quiet_s" in patch) share.quiet_s = patch.quiet_s ?? null;
    if (patch.on_open) {
      share.on_open = patch.on_open;
      if (patch.on_open !== "auto") share.wait_threshold_minutes = null;
    }
    if ("wait_threshold_minutes" in patch)
      share.wait_threshold_minutes = patch.wait_threshold_minutes ?? null;
    if (patch.rewind_depth_days !== undefined) share.rewind.depth_days = patch.rewind_depth_days;
    if (patch.rewind_depth_max_days !== undefined)
      share.rewind.max_days = patch.rewind_depth_max_days;
    if (patch.nfs_sec) {
      share.nfs_sec = patch.nfs_sec;
      share.trust_level = patch.nfs_sec.includes("sys") ? "host_trusted" : "proven";
    }
    if (patch.nfs_clients) share.nfs_clients = patch.nfs_clients;
    if (patch.root_squash !== undefined) share.root_squash = patch.root_squash;
    if (patch.host_trust_override) share.host_trust_override = patch.host_trust_override.enabled;
    bumpEtag(share);
    audit("share.settings", {
      share_id: share.share_id,
      details: patch,
    });
    return json(viewShare(share), { headers: { ETag: share.etag } });
  }),
  http.put("*/v1/shares/:id/access", async ({ params, request }) => {
    const share = shareById(String(params.id));
    if (!share) return problem(404, "not_found", "No such share.");
    const stale = etagOk(request, share.etag);
    if (stale) return stale;
    const body = (await request.json()) as {
      owners_group_sid: string;
      writers_group_sid?: string | null;
      readers_group_sid?: string | null;
    };
    if (body.owners_group_sid !== share.groups.owners.sid && !isAdmin()) {
      return problem(403, "role_required", "Only an ArchiveAdmin can change the Owners group.");
    }
    if (!isAdmin() && !isOwner(share.share_id))
      return problem(403, "forbidden", "Share Owners or ArchiveAdmin.");
    const find = (sid: string | null | undefined) =>
      sid ? (db.groups.find((g) => g.sid === sid) ?? { sid }) : null;
    share.groups = {
      owners: find(body.owners_group_sid) ?? share.groups.owners,
      writers: find(body.writers_group_sid),
      readers: find(body.readers_group_sid),
    };
    bumpEtag(share);
    audit("share.access", { share_id: share.share_id });
    return json(viewShare(share), { headers: { ETag: share.etag } });
  }),
  http.post("*/v1/shares/:id/worm/:op", async ({ params, request }) => {
    const share = shareById(String(params.id));
    if (!share) return problem(404, "not_found", "No such share.");
    if (!isAdmin()) return problem(403, "role_required", "ArchiveAdmin role required.");
    const op = String(params.op);
    if (op !== "extend") {
      const reauth = needsReauth();
      if (reauth) return reauth;
    }
    const stale = etagOk(request, share.etag);
    if (stale) return stale;
    const body = (await request.json()) as {
      confirm_name?: string;
      retention_days?: number;
      window_hours?: number;
      quiet_s_confirmed?: boolean;
    };
    if (op === "enable") {
      if (body.confirm_name !== share.name)
        return problem(
          422,
          "confirmation_mismatch",
          "The typed name doesn't match the share name.",
        );
      if (share.quiet_s != null && share.quiet_s < 60 && body.quiet_s_confirmed !== true)
        return problem(409, "worm_violation", "The share's quiet period is under 60 s.");
      const policy = activePolicy(share.dataset_id);
      if (policy?.body.mode === "smart")
        return problem(
          409,
          "layout_conflict",
          "The dataset's active policy is Smart. A WORM share can't use Smart.",
        );
      const window = body.window_hours ?? 24;
      share.worm = {
        enabled: true,
        mode: "unlocked_test",
        retention_days: body.retention_days ?? 1,
        enabled_at: iso(now()),
        lock_deadline: iso(now() + window * 3600_000),
        locked_at: null,
        policy_state: "unlocked",
      };
      audit("worm.enable", {
        share_id: share.share_id,
        details: { retention_days: body.retention_days, window_hours: window },
      });
    } else if (op === "cancel") {
      if (body.confirm_name !== share.name)
        return problem(
          422,
          "confirmation_mismatch",
          "The typed name doesn't match the share name.",
        );
      if (share.worm.mode === "locked")
        return problem(409, "worm_window_closed", "WORM is locked and can't be turned off.");
      share.worm = { enabled: false, mode: null, policy_state: "none" };
      audit("worm.cancel", { share_id: share.share_id });
    } else {
      if (
        share.worm.mode === "locked" &&
        (body.retention_days ?? 0) < (share.worm.retention_days ?? 0)
      ) {
        return problem(409, "worm_violation", "A locked retention can only go up.");
      }
      share.worm.retention_days = body.retention_days ?? share.worm.retention_days ?? null;
      audit("worm.extend", {
        share_id: share.share_id,
        details: { retention_days: body.retention_days },
      });
    }
    bumpEtag(share);
    return json(viewShare(share), { headers: { ETag: share.etag } });
  }),

  http.get("*/v1/dirs/:id/children", ({ params, request }) => {
    const dir = readableFile(String(params.id));
    if (!dir || dir.kind !== "dir") return problem(404, "not_found", "No such folder.");
    const url = new URL(request.url);
    const asOf = url.searchParams.get("as_of");
    const share = shareById(dir.share_id) as Share;
    const header = {
      file_id: dir.file_id,
      name: dir.name,
      share_id: dir.share_id,
      path: pathOf(dir),
      rewind_depth_days: share.rewind.depth_days,
    };
    const children = [...db.files.values()]
      .filter((f) => f.parent_id === dir.file_id)
      .sort((a, b) => (a.name < b.name ? -1 : a.name > b.name ? 1 : 0));
    if (!asOf) {
      const live = children.filter((f) => !f.deleted_at).map(toEntry);
      return json({ dir: header, as_of: null, ...page(live, url, 1000) });
    }
    const t = new Date(asOf).getTime();
    if (Number.isNaN(t)) return problem(400, "invalid_request", "as_of is not a date-time.");
    if (t > now() + 60_000) return problem(400, "invalid_request", "as_of is in the future.");
    if (t < now() - share.rewind.depth_days * DAY) {
      return problem(
        410,
        "beyond_rewind_depth",
        `This share keeps ${share.rewind.depth_days} days of history.`,
      );
    }
    const rows: AsOfEntry[] = [];
    for (const f of children) {
      if (new Date(f.created_at).getTime() > t) continue;
      const found = [...f.versions]
        .reverse()
        .filter((v) => new Date(v.committed_at).getTime() <= t)
        .at(-1);
      if (!found || found.kind === "tombstone") continue;
      const version = { ...found, kind: found.kind };
      rows.push({
        file_id: f.file_id,
        name: version.name,
        kind: f.kind,
        version_id: version.version_id,
        content_version_id: version.content_version_id ?? null,
        version_kind: version.kind,
        committed_at: version.committed_at,
        size: version.size ?? null,
        mtime_ns: version.mtime_ns ?? null,
        tier: version.tier ?? null,
        live: !f.deleted_at,
        current: f.versions[0]?.version_id === version.version_id,
      });
    }
    return json({ dir: header, as_of: asOf, ...page(rows, url, 1000) });
  }),
  http.get("*/v1/files/:id", ({ params }) => {
    const file = readableFile(String(params.id));
    if (!file) return problem(404, "not_found", "No such file.");
    return json(toDetail(file));
  }),
  http.get("*/v1/files/:id/versions", ({ params, request }) => {
    const file = readableFile(String(params.id));
    if (!file) return problem(404, "not_found", "No such file.");
    return json(page(file.versions, new URL(request.url)));
  }),
  http.get("*/v1/files/:id/recall-options", ({ params, request }) => {
    const file = readableFile(String(params.id));
    if (!file) return problem(404, "not_found", "No such file.");
    const versionId = new URL(request.url).searchParams.get("version_id") ?? undefined;
    return json(recallOptions(file, versionId));
  }),
  http.post("*/v1/files/:id/versions/:vid/restore", async ({ params, request }) => {
    const file = readableFile(String(params.id));
    if (!file) return problem(404, "not_found", "No such file.");
    const share = shareById(file.share_id) as Share;
    const access = myAccess(share);
    if (!access.owner && !access.writer)
      return problem(403, "forbidden", "Only the share's Owners or Writers can restore.");
    const version = file.versions.find((v) => v.version_id === String(params.vid));
    if (!version || version.kind === "tombstone" || version.state === "pruned") {
      return problem(409, "version_not_restorable", "This version can't be restored.");
    }
    const body = (await request.json()) as { target: RestoreTarget };
    const deleted = file.versions[0]?.kind === "tombstone";
    if (body.target === "overwrite" && deleted) {
      return problem(
        409,
        "version_not_restorable",
        "The file is deleted: restore it under a new name.",
      );
    }
    const name =
      body.target === "restored"
        ? restoredName(file.name)
        : body.target === "restored_timestamped"
          ? restoredTimestampedName(file.name, new Date(now()))
          : null;
    if (
      name &&
      [...db.files.values()].some(
        (f) => f.parent_id === file.parent_id && !f.deleted_at && f.name === name,
      )
    ) {
      return problem(409, "name_taken", `'${name}' already exists in that folder`);
    }
    const restore_id = newId();
    db.restores.set(restore_id, {
      state: "requested",
      requested_at: now(),
      body: {
        ...body,
        name,
        file_id: file.file_id,
        version_id: version.version_id,
        archived: version.tier?.archived ?? false,
      },
    });
    audit("version.restore", {
      file_id: file.file_id,
      version_id: version.version_id,
      share_id: file.share_id,
    });
    return json(restoreView(restore_id), { status: 202 });
  }),
  http.get("*/v1/version-restores/:id", ({ params }) => {
    if (!db.restores.has(String(params.id))) return problem(404, "not_found", "No such restore.");
    return json(restoreView(String(params.id)));
  }),
  http.post("*/v1/files/:id/versions/:vid/downloads", ({ params }) => {
    const file = readableFile(String(params.id));
    if (!file) return problem(404, "not_found", "No such file.");
    const version = file.versions.find((v) => v.version_id === String(params.vid));
    if (!version) return problem(404, "not_found", "No such version.");
    const download_id = newId();
    db.downloads.set(download_id, {
      requested_at: now(),
      file_id: file.file_id,
      version_id: version.version_id,
    });
    audit("version.download", {
      file_id: file.file_id,
      version_id: version.version_id,
      share_id: file.share_id,
    });
    if (version.tier?.archived) {
      return json(
        {
          download_id,
          file_id: file.file_id,
          version_id: version.version_id,
          state: "needs_recall",
          url: null,
          expires_at: null,
          recall_options: recallOptions(file, version.version_id),
          failure: null,
        },
        { status: 202 },
      );
    }
    return json(downloadView(download_id), { status: 202 });
  }),
  http.get("*/v1/downloads/:id", ({ params }) => {
    if (!db.downloads.has(String(params.id))) return problem(404, "not_found", "No such download.");
    return json(downloadView(String(params.id)));
  }),

  http.post("*/v1/recalls", async ({ request }) => {
    const body = (await request.json()) as {
      file_id: string;
      version_id?: string;
      priority: "standard" | "high";
      due_by?: string;
    };
    const file = readableFile(body.file_id);
    if (!file) return problem(404, "not_found", "No such file.");
    if (!(file.tier.archived && file.tier.restore_state !== "completed")) {
      return problem(409, "recall_not_needed", "This file is online; open it directly.");
    }
    const existing = activeRecallFor(file);
    if (existing) {
      existing.recall.requesters_count += 1;
      existing.requested_by = db.persona;
      return json({ ...toRecall(existing), joined: true }, { status: 200 });
    }
    const small = file.size <= NO_PROMISE_BYTES;
    const recall: MockRecall = {
      recall: {
        recall_id: newId(),
        file_id: file.file_id,
        version_id: body.version_id ?? file.versions[0]?.version_id ?? "0",
        name: file.name,
        share_id: file.share_id,
        state: "requested",
        priority: body.priority,
        eta: small ? iso(now() + (body.priority === "high" ? 1 : 15) * 3600_000) : null,
        eta_confidence: small ? "provider_sla" : "none",
        due_by: body.due_by ?? null,
        requested_at: iso(now()),
        completed_at: null,
        failure: null,
        requesters_count: 1,
      },
      log: [{ state: "requested", at: iso(now()) }],
      simulate: true,
      requested_by: db.persona,
    };
    db.recalls.unshift(recall);
    file.state = "recalling";
    audit("recall.request", {
      file_id: file.file_id,
      share_id: file.share_id,
      details: { via: "portal", priority: body.priority },
    });
    return json({ ...toRecall(recall), joined: false }, { status: 201 });
  }),
  http.get("*/v1/recalls", ({ request }) => {
    const url = new URL(request.url);
    const scope = url.searchParams.get("scope") ?? "mine";
    const shareId = url.searchParams.get("share_id");
    const state = url.searchParams.get("state");
    if (scope === "all" && !isAdmin())
      return problem(403, "role_required", "ArchiveAdmin role required.");
    if (scope === "share" && !(shareId && (isOwner(shareId) || isAdmin()))) {
      return problem(403, "forbidden", "Share Owners or ArchiveAdmin.");
    }
    const items = db.recalls
      .map(toRecall)
      .filter((r) =>
        scope === "mine"
          ? db.recalls.find((m) => m.recall.recall_id === r.recall_id)?.requested_by ===
              db.persona || r.requesters?.me === true
          : true,
      )
      .filter((r) => (scope === "share" ? r.share_id === shareId : true))
      .filter((r) => (state ? r.state === state : true));
    return json(page(items, url));
  }),
  http.get("*/v1/recalls/:id", ({ params }) => {
    const recall = db.recalls.find((r) => r.recall.recall_id === String(params.id));
    if (!recall) return problem(404, "not_found", "No such recall.");
    return json(toRecall(recall));
  }),

  http.post("*/v1/pins/preview", async ({ request }) => {
    const body = (await request.json()) as {
      items: string[];
      tier: Pin["tier"];
      override_archive?: boolean;
    };
    const files = expand(body.items);
    let fee = 0;
    let monthly = 0;
    const items: PinPreview["items"] = files.map((f) => {
      const result = pinChecks(f, body.tier, body.override_archive ?? false);
      fee += result.fee;
      monthly += result.monthly;
      return {
        file_id: f.file_id,
        name: f.name,
        checks: result.checks,
        fee: result.fee > 0 ? { amount: result.fee.toFixed(2), currency: "USD" } : null,
        monthly_cost_change: { amount: result.monthly.toFixed(2), currency: "USD" },
      };
    });
    return json({
      items,
      totals: {
        fee: { amount: fee.toFixed(2), currency: "USD" },
        monthly_cost_change: { amount: monthly.toFixed(2), currency: "USD" },
        files: files.length,
      },
    } satisfies PinPreview);
  }),
  http.post("*/v1/pins", async ({ request }) => {
    const body = (await request.json()) as {
      items: string[];
      tier: Pin["tier"];
      until?: string;
      reason: string;
      override_archive?: boolean;
    };
    const created: Pin[] = [];
    const skipped: { file_id: string; code: string; message: string }[] = [];
    for (const id of body.items) {
      const file = readableFile(id);
      if (!file) {
        skipped.push({ file_id: id, code: "not_found", message: "Not found." });
        continue;
      }
      if (!isOwner(file.share_id) && !isSteward())
        return problem(403, "forbidden", "Share Owners or DataStewards.");
      if (file.legal_hold && body.tier !== "archive") {
        skipped.push({
          file_id: id,
          code: "legal_hold",
          message: `${file.name} is under legal hold.`,
        });
        continue;
      }
      for (const p of db.pins) if (p.file_id === id && !p.released_at) p.released_at = iso(now());
      const pin: Pin = {
        pin_id: newId(),
        file_id: id,
        name: file.name,
        kind: file.kind,
        tier: body.tier,
        until: body.until ?? null,
        override_archive: body.override_archive ?? false,
        reason: body.reason,
        set_by: personRef(),
        set_at: iso(now()),
        released_at: null,
      };
      db.pins.push(pin);
      created.push(pin);
      audit("pin.create", { file_id: id, share_id: file.share_id, details: { tier: body.tier } });
    }
    return json({ pins: created, skipped }, { status: 201 });
  }),
  http.get("*/v1/pins", ({ request }) => {
    const url = new URL(request.url);
    const shareId = url.searchParams.get("share_id");
    const items = db.pins.filter(
      (p) => !p.released_at && (!shareId || db.files.get(p.file_id)?.share_id === shareId),
    );
    return json(page(items, url));
  }),
  http.post("*/v1/pins/:id/release", ({ params }) => {
    const pin = db.pins.find((p) => p.pin_id === String(params.id));
    if (!pin) return problem(404, "not_found", "No such pin.");
    pin.released_at = iso(now());
    audit("pin.release", { file_id: pin.file_id });
    return json(pin);
  }),

  http.post("*/v1/holds", async ({ request }) => {
    const body = (await request.json()) as {
      items: string[];
      kind: Hold["kind"];
      until?: string;
      reason: string;
    };
    if (body.kind === "legal") {
      if (!isAdmin()) return problem(403, "role_required", "ArchiveAdmin role required.");
      const reauth = needsReauth();
      if (reauth) return reauth;
    }
    const holds: Hold[] = [];
    for (const id of body.items) {
      const file = readableFile(id);
      if (!file) continue;
      if (body.kind === "keep_online" && !isOwner(file.share_id) && !isSteward()) {
        return problem(403, "forbidden", "Share Owners or DataStewards.");
      }
      const hold: Hold = {
        hold_id: newId(),
        file_id: id,
        name: file.name,
        kind: body.kind,
        until: body.until ?? null,
        reason: body.reason,
        set_by: personRef(),
        set_at: iso(now()),
        released_at: null,
      };
      db.holds.push(hold);
      holds.push(hold);
      audit(body.kind === "legal" ? "hold.legal.set" : "hold.keep_online.set", {
        file_id: id,
        share_id: file.share_id,
      });
    }
    return json({ holds, skipped: [] }, { status: 201 });
  }),
  http.get("*/v1/holds", ({ request }) => {
    const url = new URL(request.url);
    const shareId = url.searchParams.get("share_id");
    const kind = url.searchParams.get("kind");
    const fileId = url.searchParams.get("file_id");
    const target = fileId ? readableFile(fileId) : null;
    if (fileId && !target) return problem(404, "not_found", "No such file.");
    const applies = target
      ? new Set([target.file_id, ...ancestors(target).map((a) => a.file_id)])
      : null;
    const items = db.holds.filter(
      (h) =>
        !h.released_at &&
        (!kind || h.kind === kind) &&
        (!shareId || db.files.get(h.file_id)?.share_id === shareId) &&
        (!applies || applies.has(h.file_id)) &&
        !!readableFile(h.file_id),
    );
    return json(
      page(
        items.map((h) => ({
          ...h,
          inherited_from: target && h.file_id !== target.file_id ? h.file_id : null,
        })),
        url,
      ),
    );
  }),
  http.post("*/v1/holds/:id/release", ({ params }) => {
    const hold = db.holds.find((h) => h.hold_id === String(params.id));
    if (!hold) return problem(404, "not_found", "No such hold.");
    if (hold.kind === "legal") {
      if (!isAdmin()) return problem(403, "role_required", "ArchiveAdmin role required.");
      const reauth = needsReauth();
      if (reauth) return reauth;
    }
    hold.released_at = iso(now());
    audit(hold.kind === "legal" ? "hold.legal.release" : "hold.keep_online.release", {
      file_id: hold.file_id,
    });
    return json(hold);
  }),

  http.get("*/v1/datasets", ({ request }) => {
    if (!isAdmin() && !isSteward())
      return problem(403, "role_required", "ArchiveAdmin or DataSteward.");
    const items = db.datasets.map((d) => ({
      ...d,
      pending_proposals: db.proposals.filter(
        (p) => p.proposal.dataset_id === d.dataset_id && p.proposal.state === "pending",
      ).length,
    }));
    return json(page(items, new URL(request.url)));
  }),
  http.post("*/v1/datasets", async ({ request }) => {
    if (!isAdmin()) return problem(403, "role_required", "ArchiveAdmin only.");
    const b = (await request.json()) as Partial<NewDataset>;
    const bad = (field: string, message: string) =>
      problem(400, "invalid_request", `${field}: ${message}`, { errors: [{ field, message }] });
    const name = b.name ?? "";
    if (!/^[A-Za-z0-9][A-Za-z0-9._-]{0,62}$/.test(name)) {
      return bad("name", "1 to 63 letters, digits, '.', '_' or '-'");
    }
    const backend = db.backends.find((x) => x.backend_id === b.backend_id);
    if (!backend) return bad("backend_id", "no such backend");
    if (!backend.can_be_home) {
      return bad(
        "backend_id",
        "a dataset's home is Azure Blob; register S3 data as a migration source",
      );
    }
    const edge = mockEdges().find((e) => e.edge_id === b.edge_id);
    if (!edge || edge.state === "revoked") return bad("edge_id", "no such edge");
    const prefix = b.key_prefix ?? "";
    if (!/^(?:[A-Za-z0-9!_.*'()-]+\/)*$/.test(prefix)) {
      return bad("key_prefix", "empty, or path segments each ending in '/'");
    }
    if (db.datasets.some((d) => d.name === name)) {
      return problem(409, "name_taken", `a dataset named ${name} exists`);
    }
    const dataset = {
      dataset_id: newId(),
      name,
      backend_id: backend.backend_id,
      backend_kind: backend.kind,
      private_network: backend.private_network,
      edge_id: edge.edge_id,
      key_prefix: prefix,
      root_dir_id: newId(),
      storage_layout: b.storage_layout ?? "two_account",
      migration_state: "steady",
      shares: [],
      active_policy: null,
      pending_proposals: 0,
    };
    db.datasets.push(dataset);
    audit("dataset.add", { dataset_id: dataset.dataset_id });
    return json(dataset, { status: 201 });
  }),
  http.get("*/v1/backends", () => {
    if (!isAdmin() && !isSteward())
      return problem(403, "role_required", "ArchiveAdmin or DataSteward.");
    return json({ backends: [...db.backends].sort((a, b) => a.name.localeCompare(b.name)) });
  }),
  http.get("*/v1/datasets/:id/policy", ({ params }) => {
    const policy = activePolicy(String(params.id));
    return policy ? json(policy) : problem(404, "not_found", "This dataset has no policy yet.");
  }),
  http.get("*/v1/datasets/:id/policies", ({ params, request }) =>
    json(
      page(
        db.policies.filter((p) => p.dataset_id === String(params.id)),
        new URL(request.url),
      ),
    ),
  ),
  http.post("*/v1/datasets/:id/policies", async ({ params, request }) => {
    if (!isAdmin()) return problem(403, "role_required", "ArchiveAdmin role required.");
    const datasetId = String(params.id);
    const body = (await request.json()) as {
      body: PolicyBody;
      base_version: number | null;
      note?: string;
    };
    const active = activePolicy(datasetId);
    if ((active?.version ?? null) !== body.base_version) {
      return problem(
        409,
        "version_conflict",
        `Version ${active?.version ?? "none"} is active now; you started from ${body.base_version ?? "none"}.`,
      );
    }
    const dataset = db.datasets.find((d) => d.dataset_id === datasetId);
    if (!dataset) return problem(404, "not_found", "No such dataset.");
    const preview = mockPolicyPreview(
      body.body,
      dataset,
      db.shares.filter((s) => s.dataset_id === datasetId),
    );
    if (!preview.valid) {
      return problem(422, "policy_invalid", "The policy has errors.", {
        checks: preview.checks.filter((c) => c.severity === "error"),
      });
    }
    for (const p of db.proposals) {
      if (p.proposal.dataset_id === datasetId && p.proposal.state === "pending")
        p.proposal.stale = true;
    }
    return json(createPolicyVersion(datasetId, body.body, body.note ?? null, null), {
      status: 201,
    });
  }),
  http.post("*/v1/policies/preview", async ({ request }) => {
    const body = (await request.json()) as {
      dataset_id: string;
      body: PolicyBody;
      share_overrides?: [];
      assumptions?: object;
    };
    const dataset = db.datasets.find((d) => d.dataset_id === body.dataset_id);
    if (!dataset) return problem(404, "not_found", "No such dataset.");
    return json(
      mockPolicyPreview(
        body.body,
        dataset,
        db.shares.filter((s) => s.dataset_id === dataset.dataset_id),
        body.share_overrides,
        body.assumptions,
      ),
    );
  }),
  http.post("*/v1/policies/proposals", async ({ request }) => {
    if (!isAdmin() && !isSteward())
      return problem(403, "role_required", "ArchiveAdmin or DataSteward.");
    const body = (await request.json()) as {
      dataset_id: string;
      body: PolicyBody;
      rationale: string;
      base_version?: number | null;
      share_overrides?: ShareOverride[];
    };
    const proposal: Proposal = {
      proposal_id: newId(),
      dataset_id: body.dataset_id,
      base_version: body.base_version ?? null,
      stale: false,
      body: body.body,
      rationale: body.rationale,
      proposed_by: personRef(),
      state: "pending",
      decided_by: null,
      decided_at: null,
      decision_note: null,
      created_at: iso(now()),
      share_overrides: body.share_overrides ?? [],
    };
    db.proposals.unshift({ proposal, proposer: db.persona });
    audit("policy.propose", {
      dataset_id: body.dataset_id,
      details: { proposal_id: proposal.proposal_id },
    });
    return json(proposal, { status: 201 });
  }),
  http.get("*/v1/policies/proposals", ({ request }) => {
    const url = new URL(request.url);
    const datasetId = url.searchParams.get("dataset_id");
    const state = url.searchParams.get("state");
    const items = db.proposals
      .filter(
        (p) =>
          (!datasetId || p.proposal.dataset_id === datasetId) &&
          (!state || p.proposal.state === state),
      )
      .map((p) => proposalView(p, false));
    return json(page(items, url));
  }),
  http.get("*/v1/policies/proposals/:id", ({ params }) => {
    const entry = db.proposals.find((p) => p.proposal.proposal_id === String(params.id));
    return entry ? json(proposalView(entry, true)) : problem(404, "not_found", "No such proposal.");
  }),
  http.post("*/v1/policies/proposals/:id/:op", async ({ params, request }) => {
    const entry = db.proposals.find((p) => p.proposal.proposal_id === String(params.id));
    if (!entry) return problem(404, "not_found", "No such proposal.");
    const op = String(params.op);
    const p = entry.proposal;
    if (p.state !== "pending")
      return problem(409, "proposal_not_pending", `This proposal was already ${p.state}.`);
    if (op === "withdraw") {
      if (entry.proposer !== db.persona)
        return problem(403, "forbidden", "Only the proposer can withdraw it.");
      p.state = "withdrawn";
      p.decided_at = iso(now());
      return json(proposalView(entry, false));
    }
    if (!isAdmin()) return problem(403, "role_required", "ArchiveAdmin role required.");
    const body = (await request.json().catch(() => ({}))) as {
      note?: string;
      accept_stale?: boolean;
    };
    if (op === "reject") {
      p.state = "rejected";
      p.decided_by = personRef();
      p.decided_at = iso(now());
      p.decision_note = body.note ?? null;
      audit("policy.proposal.reject", {
        dataset_id: p.dataset_id,
        details: { proposal_id: p.proposal_id },
      });
      return json(proposalView(entry, false));
    }
    if (entry.proposer === db.persona)
      return problem(
        403,
        "self_approval",
        "You proposed this, so another ArchiveAdmin has to approve it.",
      );
    const view = proposalView(entry, true);
    if (view.stale && !body.accept_stale)
      return problem(
        409,
        "version_conflict",
        "The active policy changed since this proposal was made.",
      );
    if (view.preview && !view.preview.valid) {
      return problem(422, "policy_invalid", "The proposal has errors.", {
        checks: view.preview.checks.filter((c) => c.severity === "error"),
      });
    }
    const policy = createPolicyVersion(p.dataset_id, p.body, body.note ?? null, p.proposal_id);
    // The proposal's "what users see" answers reach the shares only now (anti-pattern 7).
    for (const o of p.share_overrides ?? []) {
      const share = shareById(o.share_id);
      if (!share) continue;
      if (o.on_open) share.on_open = o.on_open;
      share.wait_threshold_minutes =
        share.on_open === "auto"
          ? (o.wait_threshold_minutes ?? share.wait_threshold_minutes)
          : null;
      bumpEtag(share);
      audit("share.settings", {
        share_id: share.share_id,
        details: { proposal_id: p.proposal_id },
      });
    }
    p.state = "approved";
    p.decided_by = personRef();
    p.decided_at = iso(now());
    p.decision_note = body.note ?? null;
    return json({ proposal: proposalView(entry, false), policy });
  }),

  http.get("*/v1/audit", ({ request }) => {
    const url = new URL(request.url);
    const shareId = url.searchParams.get("share_id");
    if (!isAdmin()) {
      if (!shareId) return problem(400, "invalid_request", "Owners must choose a share.");
      if (!isOwner(shareId)) return problem(403, "forbidden", "Share Owners or ArchiveAdmin.");
    }
    const f = (k: string) => url.searchParams.get(k);
    const items = db.audit
      .filter((a) => !shareId || a.share_id === shareId)
      .filter((a) => !f("file_id") || a.file_id === f("file_id"))
      .filter((a) => !f("actor") || a.actor === f("actor"))
      .filter((a) => !f("action") || a.action.startsWith(f("action") ?? ""))
      .filter((a) => !f("from") || a.at >= (f("from") ?? ""))
      .filter((a) => !f("to") || a.at <= (f("to") ?? ""))
      .map((a) => (isAdmin() ? a : { ...a, client_addr: null }));
    return json(page(items, url));
  }),

  // Source backends, sources, migrations and the cutover report: mocks/migrations.ts.
  ...migrationHandlers({
    state: () => db.migration,
    datasets: () => db.datasets,
    isAdmin,
    needsReauth,
    problem,
    json,
    newId,
    identityId: () => me().identity_id,
    hasSid: () => !!me().sid,
    audit,
  }),
  ...consoleHandlers(
    {
      state: () => db.migration,
      datasets: () => db.datasets,
      isAdmin,
      needsReauth,
      problem,
      json,
      newId,
      identityId: () => me().identity_id,
      hasSid: () => !!me().sid,
      audit,
    },
    () => db.migration.console,
  ),

  http.get("*/v1/locations", () => {
    if (!isAdmin()) return problem(403, "role_required", "ArchiveAdmin only.");
    const ds = db.datasets[0]?.dataset_id ?? "2";
    return json({
      items: [
        { ...example.Location, dataset_id: ds },
        {
          ...example.Location,
          migration_id: "6",
          dataset_id: ds,
          prefix: "raw/2018/",
          state: "cut_over",
          files: { total: 900, verified: 900, failed: 0 },
          cut_over_at: "2026-10-14T18:00:00.000Z",
        },
      ],
      next_cursor: null,
    });
  }),

  http.post("*/v1/edges", async ({ request }) => {
    if (!isAdmin()) return problem(403, "role_required", "ArchiveAdmin only.");
    const body = (await request.json()) as { name?: string };
    const name = body.name ?? "";
    if (!/^[a-z][a-z0-9-]{0,62}$/.test(name)) {
      return problem(400, "invalid_request", "name: lowercase letters, digits or hyphens");
    }
    if (name === "edge-houston-1")
      return problem(409, "name_taken", `an edge named ${name} exists`);
    const payload = btoa(
      JSON.stringify({
        url: "https://enrol.mock",
        server_ca_sha256: "ab".repeat(32),
        token: "mock-token",
      }),
    );
    addedEdges.set("77", { name, created: now(), polls: 0 });
    return json(
      {
        edge_id: "77",
        name,
        bundle: "ofe1." + payload.replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, ""),
        expires_at: new Date(now() + 24 * 3600_000).toISOString(),
      },
      { status: 201 },
    );
  }),

  http.post("*/v1/edges/:id/bundle", ({ params }) => {
    if (!isAdmin()) return problem(403, "role_required", "ArchiveAdmin only.");
    return json(
      {
        edge_id: String(params.id),
        name: "new-edge",
        bundle: "ofe1.bW9jay1yZWlzc3Vl",
        expires_at: new Date(now() + 24 * 3600_000).toISOString(),
      },
      { status: 201 },
    );
  }),

  http.post("*/v1/edges/:id/revoke", ({ params }) => {
    if (!isAdmin()) return problem(403, "role_required", "ArchiveAdmin only.");
    const edge = mockEdges().find((e) => e.edge_id === String(params.id));
    if (!edge) return problem(404, "not_found", "no such edge");
    revokedEdges.add(edge.edge_id);
    return json({ edge_id: edge.edge_id, name: edge.name, state: "revoked" });
  }),

  http.get("*/v1/edges", () => {
    if (!isAdmin()) return problem(403, "role_required", "ArchiveAdmin only.");
    return json({ edges: listedEdges() });
  }),

  http.get("*/v1/activity", ({ request }) => {
    if (!isAdmin()) return problem(403, "role_required", "ArchiveAdmin only.");
    const url = new URL(request.url);
    const days = Number(url.searchParams.get("days") ?? "30");
    if (!Number.isInteger(days) || days < 1 || days > 90) {
      return problem(400, "invalid_request", "days must be 1 to 90.");
    }
    const end = new Date(now());
    end.setUTCHours(0, 0, 0, 0);
    end.setUTCDate(end.getUTCDate() + 1);
    const rows = Array.from({ length: days }, (_, i) => {
      const day = new Date(end.getTime() - (days - i) * 86_400_000);
      const n = (i * 7) % 5;
      return {
        date: day.toISOString().slice(0, 10),
        uploads: { files: n * 3, bytes: n * 3 * 184 * 1024 ** 3 },
        recalls: { started: n % 3, available: n % 2, failed: i === days - 2 ? 1 : 0 },
      };
    });
    const sum = (f: (d: (typeof rows)[number]) => number) => rows.reduce((t, d) => t + f(d), 0);
    return json({
      from: new Date(end.getTime() - days * 86_400_000).toISOString(),
      to: end.toISOString(),
      dataset_id: url.searchParams.get("dataset_id"),
      days: rows,
      uploads: {
        files: sum((d) => d.uploads.files),
        bytes: sum((d) => d.uploads.bytes),
        unverified: 2,
        landing: 1,
      },
      recalls: {
        open: db.recalls.filter(
          (r) => !["available", "failed", "cancelled", "superseded"].includes(r.recall.state),
        ).length,
        available: sum((d) => d.recalls.available),
        failed: sum((d) => d.recalls.failed),
        bytes: sum((d) => d.recalls.available) * 184 * 1024 ** 3,
        time_to_available_s: { p50: 5 * 3600, p90: 14 * 3600 },
      },
    });
  }),

  http.get("*/healthz", () => json({ status: "ok" })),

  // The visual views (portal-api.md 5.22), in ./visual.ts.
  ...visualHandlers({
    db: () => db,
    me,
    isAdmin,
    isSteward,
    myAccess,
    readableFile,
    ancestors,
    recallState: (r) => advance(r).recall.state,
    problem,
  }),
  // Connect Azure and AWS, Storage Mover deployment, source scans: before Settings, so
  // their GET /v1/settings/storage-mover (with the deployment) answers.
  ...cloudHandlers(() => db),
  ...settingsHandlers(() => db),
  ...makeAnnexHandlers(() => me()),
];

function restoreView(id: string): VersionRestore {
  const r = db.restores.get(id);
  if (!r) throw new Error("no restore");
  const body = r.body as {
    target: RestoreTarget;
    name: string | null;
    file_id: string;
    version_id: string;
    archived: boolean;
  };
  const elapsed = now() - r.requested_at;
  const state: VersionRestore["state"] = body.archived
    ? "waiting_recall"
    : elapsed < 1500
      ? "requested"
      : elapsed < 3500
        ? "copying"
        : "done";
  return {
    restore_id: id,
    file_id: body.file_id,
    version_id: body.version_id,
    target: body.target,
    name: body.name ?? null,
    state,
    recall_id: body.archived ? "501" : null,
    result_file_id: state === "done" ? body.file_id : null,
    result_version_id: state === "done" ? "99999" : null,
    failure: null,
    requested_at: iso(r.requested_at),
    updated_at: iso(now()),
  };
}

function downloadView(id: string) {
  const d = db.downloads.get(id);
  if (!d) throw new Error("no download");
  const ready = now() - d.requested_at > 1200;
  return {
    download_id: id,
    file_id: d.file_id,
    version_id: d.version_id,
    state: ready ? ("ready" as const) : ("preparing" as const),
    url: ready
      ? `https://omnifilelive.privatelink.blob.core.windows.net/sim/${d.file_id}?sv=mock`
      : null,
    expires_at: ready ? iso(d.requested_at + 10 * 60_000) : null,
    recall_options: null,
    failure: null,
  };
}
