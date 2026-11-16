import { http, HttpResponse, type JsonBodyType } from "msw";
import type { components } from "../api/schema.gen";

/**
 * MSW handlers for filers and annexes (portal-api.md 5.20). The shared guard
 * in handlers.ts runs first (CSRF, Idempotency-Key, the request log).
 */

type Filer = components["schemas"]["Filer"];
type AnnexDetail = components["schemas"]["AnnexDetail"];
type AnnexFile = components["schemas"]["AnnexFile"];
type AnnexEvent = components["schemas"]["AnnexEvent"];
type Who = { roles: string[]; groups?: { sid: string }[] | string[] };
type Ruleset = components["schemas"]["AnnexRuleset"];
type DryRun = components["schemas"]["AnnexDryRun"];
type RuleBody = components["schemas"]["AnnexRuleBody"];
type Placement = components["schemas"]["AnnexPlacement"];
type ReturnTargets = components["schemas"]["ReturnTargets"];
type RefusedCopy = components["schemas"]["AnnexProblems"]["copies_refused"] extends
  (infer T)[] | undefined
  ? T
  : never;

const DAY = 86_400_000;
const iso = (ms: number) => new Date(ms).toISOString();
const json = (body: unknown, init?: ResponseInit) => HttpResponse.json(body as JsonBodyType, init);
const problem = (status: number, code: string, detail: string) =>
  HttpResponse.json(
    { type: `urn:omnifile:error:${code}`, title: code.replace(/_/g, " "), status, code, detail },
    { status, headers: { "Content-Type": "application/problem+json" } },
  );

const OWNERS = "S-1-5-21-1000-2000-3000-1201";

type State = {
  filers: Filer[];
  annexes: AnnexDetail[];
  files: Map<string, AnnexFile[]>;
  events: Map<string, AnnexEvent[]>;
  /** Decision 63: the folders above each annex's files, read so far. */
  readSets: Map<
    string,
    {
      ready: boolean;
      filer_answered: boolean;
      read_at: string | null;
      folders: number;
      unreadable: number;
    }
  >;
  next: number;
  defaults: {
    quiet_minutes: number;
    quick_scan_minutes: number;
    full_scan_minutes: number;
    reconcile_days: number;
  };
  defaultsVersion: number;
  rulesets: Map<string, (Ruleset & { body: RuleBody })[]>;
  dryRuns: Map<string, DryRun>;
  placements: Map<string, Placement>;
  /** Copies Azure's read-back refuses, per annex (#81's follow-up). */
  refused: Map<string, RefusedCopy[]>;
  /** How many there are in all, when the list is cut short. */
  refusedTotal: Map<string, number>;
};

const COPY_BODY: RuleBody = {
  template: "copy",
  rules: [
    { id: "copy", trigger: "arrived", conditions: {}, actions: [{ copy_to: { kind: "tier" } }] },
    { id: "recopy", trigger: "changed", conditions: {}, actions: [{ copy_to: { kind: "tier" } }] },
    { id: "keep", trigger: "deleted_at_source", conditions: {}, actions: [{ keep_copy: true }] },
  ],
};

type MockRule = {
  id?: string;
  trigger: string;
  conditions?: Record<string, unknown>;
  actions?: Record<string, unknown>[];
};

function removing(body: RuleBody): boolean {
  return (body.rules as MockRule[]).some((r) =>
    (r.actions ?? []).some((a) => "remove_from_source" in a || "remove_after_days" in a),
  );
}

/** A small stand-in for the edge's dry run: the mock files against the rules. */
function report(body: RuleBody, files: AnnexFile[]) {
  const rules = (body.rules as MockRule[]).map((r) => ({
    id: r.id ?? r.trigger,
    trigger: r.trigger,
    files: 0,
    bytes: 0,
    samples: [] as string[],
  }));
  let copied = 0;
  let copiedBytes = 0;
  let excluded = 0;
  for (const f of files.filter((x) => !x.gone_since)) {
    const i = (body.rules as MockRule[]).findIndex((r) => {
      if (r.trigger !== "arrived") return false;
      const ext = r.conditions?.extension as string[] | undefined;
      return !ext || ext.some((e) => f.path.toLowerCase().endsWith(`.${e}`));
    });
    const hit = rules[i];
    if (!hit) continue;
    hit.files += 1;
    hit.bytes += f.size;
    if (hit.samples.length < 20) hit.samples.push(f.path);
    const r = (body.rules as MockRule[])[i];
    if ((r?.actions ?? []).some((a) => "exclude" in a)) excluded += 1;
    else {
      copied += 1;
      copiedBytes += f.size;
    }
  }
  const leaving = removing(body) ? copied : 0;
  return {
    files: files.length,
    rules,
    copied: { files: copied, bytes: copiedBytes },
    excluded: { files: excluded, bytes: 0 },
    undecided: { files: 0, bytes: 0 },
    removals: {
      files: leaving,
      bytes: leaving ? copiedBytes : 0,
      now: { files: 0, bytes: 0 },
      by_day: leaving
        ? [{ date: iso(Date.now() + 30 * DAY).slice(0, 10), files: leaving, bytes: copiedBytes }]
        : [],
    },
  };
}

let state: State = fresh();

function fresh(now: number = Date.now()): State {
  const filer: Filer = {
    filer_id: "41",
    name: "ontap01",
    vendor: "ontap",
    host: "ontap01.corp.example",
    protocol: "smb",
    export: "geo",
    edge_id: "1",
    credential_mode: "machine_account",
    host_trusted: false,
    event_source: "smb_notify",
    health: { ok: true, read_only: true, readable: true, sec: "krb5", error: null },
    health_checked_at: iso(now - 60_000),
    created_at: iso(now - 30 * DAY),
    access: {
      answered: true,
      limited: false,
      groups: [],
      confirmed_by: "user:admin@corp.example",
      confirmed_at: iso(now - 30 * DAY),
    },
    acls: {
      hidden_since: iso(now - DAY),
      acknowledged_by: null,
      acknowledged_at: null,
      blocks_grants: true,
    },
  };
  const annex: AnnexDetail = {
    annex_id: "7",
    name: "long-term-storage",
    mode: "copy",
    state: "active",
    filer_id: "41",
    filer_name: "ontap01",
    unc: "\\\\ontap01.corp.example\\geo\\long-term-storage",
    path: "long-term-storage",
    dataset_id: "3",
    dataset_name: "geo-annex",
    dataset_path: "",
    edge_id: "1",
    owners_sid: OWNERS,
    settings: {
      quiet_minutes: 10,
      quick_scan_minutes: 60,
      full_scan_minutes: 1440,
      reconcile_days: 7,
    },
    deleted_retention_days: null,
    ruleset_version: 1,
    ruleset_state: "active",
    pending_proposals: 0,
    removals: {
      paused: false,
      dry_run_until: null,
      rate_per_hour: 1000,
      window: null,
      grace_minutes: 15,
      pointer_files: false,
      enabled: false,
    },
    host_trusted: false,
    baseline: { at: iso(now - 20 * DAY), files: 14210, bytes: 96 * 1024 ** 4 },
    last_quick_scan_at: iso(now - 20 * 60_000),
    last_full_scan_at: iso(now - 5 * 3600_000),
    last_reconcile_at: iso(now - 3 * DAY),
    last_event_at: iso(now - 60_000),
    created_at: iso(now - 20 * DAY),
    entity_version: 4,
    counts: {
      present: 3,
      copied: 2,
      verified: 2,
      pending: 1,
      gone: 1,
      purged: 0,
      bytes: 184 * 1024 ** 3,
      bytes_verified: 180 * 1024 ** 3,
      problems_30d: 2,
    },
    filer,
    can_manage: true,
    can_purge: true,
  };
  const file = (id: string, path: string, extra: Partial<AnnexFile>): AnnexFile => ({
    file_id: id,
    path,
    size: 1024 ** 3,
    mtime_ns: String((now - 21 * DAY) * 1e6),
    nas_state: "present",
    copy_state: "verified",
    tier: "warm",
    first_seen: iso(now - 20 * DAY),
    last_seen: iso(now - 5 * 3600_000),
    gone_since: null,
    baseline: true,
    copied_at: iso(now - 20 * DAY),
    verified_at: iso(now - 20 * DAY),
    version_id: id + "1",
    purge_requested_at: null,
    purged_at: null,
    ...extra,
  });
  const files = [
    file("101", "dataset/line_1042.segy", { size: 184 * 1024 ** 3, tier: "archive" }),
    file("102", "dataset/notes.txt", {
      size: 4096,
      copy_state: "changed",
      baseline: false,
      folders_narrow: ["long-term-storage/dataset"],
    }),
    file("103", "dataset/new.segy", {
      copy_state: "pending",
      tier: null,
      baseline: false,
      version_id: null,
      copied_at: null,
      verified_at: null,
      first_seen: iso(now - 600_000),
    }),
    file("104", "dataset/old.segy", {
      nas_state: "gone",
      gone_since: iso(now - 2 * DAY),
    }),
  ];
  const ev = (id: string, kind: string, ago: number, extra: Partial<AnnexEvent>): AnnexEvent => ({
    event_id: id,
    at: iso(now - ago),
    kind,
    path: null,
    file_id: null,
    version_id: null,
    size: null,
    observed_at: null,
    details: {},
    ...extra,
  });
  const events = [
    ev("9001", "arrived", 2 * DAY, { path: "dataset/notes.txt", file_id: "102", size: 4096 }),
    ev("9002", "left", 2 * DAY, { path: "dataset/old.segy", file_id: "104" }),
    ev("9003", "copied", 2 * DAY, { path: "dataset/notes.txt", file_id: "102", size: 4096 }),
    ev("9004", "arrived", 600_000, { path: "dataset/new.segy", file_id: "103", size: 1024 ** 3 }),
    ev("9005", "skipped", 3 * 3600_000, {
      path: "dataset/locked.bin",
      details: { reason: "unreadable", detail: "Permission denied" },
    }),
  ];
  return {
    filers: [
      filer,
      {
        ...filer,
        filer_id: "42",
        name: "isilon02",
        vendor: "powerscale",
        host: "isilon02.corp.example",
        protocol: "nfs4",
        export: "/ifs/seismic",
        edge_id: "4",
        event_source: "scan",
        health: {
          ok: false,
          read_only: null,
          readable: false,
          sec: null,
          error: "mount timed out",
        },
        health_checked_at: iso(now - 50 * 60_000),
        created_at: iso(now - 3 * DAY),
      },
    ],
    annexes: [annex],
    files: new Map([["7", files]]),
    events: new Map([["7", events]]),
    readSets: new Map(),
    refusedTotal: new Map(),
    refused: new Map([
      [
        "7",
        [
          {
            file_id: "103",
            path: "dataset/new.segy",
            at_bound: true,
            alerting: true,
            copy_refusals: 3,
            copy_refused_at: iso(now - 2 * DAY),
            copy_stopped: null,
            copy_refused_ack: null,
          },
        ],
      ],
    ]),
    next: 500,
    defaults: {
      quiet_minutes: 10,
      quick_scan_minutes: 60,
      full_scan_minutes: 1440,
      reconcile_days: 7,
    },
    defaultsVersion: 1,
    rulesets: new Map([
      [
        "7",
        [
          {
            version: 1,
            state: "active",
            mode: "copy",
            removes: false,
            needs_archive_admin: false,
            owner_identity_id: "11",
            owner_name: "Ana Admin",
            proposed_via: "portal",
            proposed_by_app: null,
            rationale: null,
            created_at: iso(now - 20 * DAY),
            decided_at: iso(now - 20 * DAY),
            decided_by_identity_id: "11",
            decision_note: null,
            dry_run_id: null,
            body: COPY_BODY,
          },
        ],
      ],
    ]),
    dryRuns: new Map(),
    placements: new Map(),
  };
}

/** The return targets the mock offers: back where it came from (the
 * requester may add files there), and a share they can't write to. */
function returnTargets(annexId: string, f: AnnexFile): ReturnTargets {
  const folder = f.path.includes("/") ? f.path.slice(0, f.path.lastIndexOf("/")) : "";
  const name = f.path.slice(f.path.lastIndexOf("/") + 1);
  const unc = `\\\\ontap01.corp.example\\geo\\long-term-storage\\${f.path.replace(/\//g, "\\")}`;
  return {
    file_id: f.file_id,
    annex_id: annexId,
    nas_state: f.nas_state,
    rule: "back",
    ruleset_version: 1,
    items: [
      {
        location: { kind: "nas_folder", origin: true },
        default: true,
        on_conflict: "restored_name",
        hold_days: 14,
        allowed: true,
        why_not: null,
        target: {
          kind: "nas",
          folder: `long-term-storage/${folder}`,
          name,
          filer_id: "41",
          origin: true,
        },
        display: unc,
        name_taken: false,
        free_bytes: 63 * 1024 ** 4,
        space_seen_at: iso(Date.now()),
      },
      {
        location: { kind: "share_folder", share_id: "1", path: "recalls" },
        default: false,
        on_conflict: "overwrite_keep_version",
        hold_days: 0,
        allowed: false,
        why_not: "the requester isn't an Owner or Writer of that share",
        target: { kind: "share", folder: `recalls/${folder}`, name, share_id: "1" },
        display: `scratch:/recalls/${f.path}`,
        name_taken: false,
        free_bytes: null,
        space_seen_at: null,
      },
    ],
  };
}

export function resetAnnexMock(now?: number): void {
  state = fresh(now);
}

export function getAnnexMock(): State {
  return state;
}

function summary(a: AnnexDetail) {
  const { counts: _c, filer: _f, can_manage: _m, can_purge: _p, ...rest } = a;
  return rest;
}

export function makeAnnexHandlers(me: () => Who) {
  const roles = () => me().roles;
  const admin = () => roles().includes("ArchiveAdmin");
  const steward = () => roles().includes("DataSteward");
  const find = (id: string) => state.annexes.find((a) => a.annex_id === id);
  return [
    http.get("*/v1/filers", () => {
      if (!admin() && !steward())
        return problem(403, "role_required", "ArchiveAdmin or DataSteward.");
      return json({ items: state.filers, next_cursor: null });
    }),
    http.post("*/v1/filers", async ({ request }) => {
      if (!admin()) return problem(403, "role_required", "ArchiveAdmin only.");
      const b = (await request.json()) as components["schemas"]["FilerCreate"];
      if (state.filers.some((f) => f.name === b.name))
        return problem(409, "name_taken", `filer ${b.name} exists`);
      const filer: Filer = {
        filer_id: String(++state.next),
        name: b.name,
        vendor: b.vendor ?? "generic",
        host: b.host,
        protocol: b.protocol,
        export: b.export,
        edge_id: b.edge_id,
        credential_mode: b.credential_mode ?? "machine_account",
        host_trusted: b.credential_mode === "sec_sys",
        event_source: b.event_source ?? (b.protocol === "smb" ? "smb_notify" : "scan"),
        health: null,
        health_checked_at: null,
        created_at: iso(Date.now()),
      };
      state.filers.push(filer);
      return json(filer, { status: 201 });
    }),
    http.get("*/v1/filers/:id/health", ({ params }) => {
      const f = state.filers.find((x) => x.filer_id === String(params.id));
      if (!f) return problem(404, "not_found", "no such filer");
      return json({ filer_id: f.filer_id, checked_at: f.health_checked_at, ok: null, annexes: 0 });
    }),
    http.get("*/v1/filers/:id", ({ params }) => {
      const f = state.filers.find((x) => x.filer_id === String(params.id));
      return f ? json(f, { headers: { ETag: '"1"' } }) : problem(404, "not_found", "no such filer");
    }),
    http.patch("*/v1/filers/:id", async ({ params, request }) => {
      // Decision 65: an ArchiveAdmin answers whether the share limits access.
      if (!admin()) return problem(403, "role_required", "ArchiveAdmin only.");
      if (!request.headers.get("If-Match")) return problem(428, "if_match_required", "If-Match");
      const f = state.filers.find((x) => x.filer_id === String(params.id));
      if (!f) return problem(404, "not_found", "no such filer");
      const b = (await request.json()) as {
        access?: { limited: boolean; groups?: string[] };
        acls_hidden_acknowledged?: boolean;
      };
      if (b.acls_hidden_acknowledged) {
        f.acls = {
          hidden_since: f.acls?.hidden_since ?? null,
          acknowledged_by: "user:admin@corp.example",
          acknowledged_at: iso(Date.now()),
          blocks_grants: false,
        };
        for (const a of state.annexes) if (a.filer.filer_id === f.filer_id) a.filer = { ...f };
      }
      if (b.access) {
        f.access = {
          answered: true,
          limited: b.access.limited,
          groups: b.access.groups ?? [],
          confirmed_by: "user:admin@corp.example",
          confirmed_at: iso(Date.now()),
        };
        for (const a of state.annexes) if (a.filer.filer_id === f.filer_id) a.filer = { ...f };
      }
      return json(f, { headers: { ETag: '"2"' } });
    }),
    http.get("*/v1/annexes", () => json({ items: state.annexes.map(summary), next_cursor: null })),
    http.post("*/v1/annexes", async ({ request }) => {
      if (!admin() && !steward())
        return problem(403, "role_required", "ArchiveAdmin or DataSteward.");
      const b = (await request.json()) as components["schemas"]["AnnexCreate"];
      if ((b.mode ?? "copy") !== "copy")
        return problem(422, "mode_not_available", `${b.mode} comes with a later release`);
      const f = state.filers.find((x) => x.filer_id === b.filer_id);
      if (!f) return problem(400, "invalid_request", "filer_id: no such filer");
      const now = Date.now();
      const base = fresh(now).annexes[0] as AnnexDetail;
      const a: AnnexDetail = {
        ...base,
        mode: "copy",
        annex_id: String(++state.next),
        name: b.name,
        state: "registered",
        filer_id: f.filer_id,
        filer_name: f.name,
        unc:
          f.protocol === "smb"
            ? `\\\\${f.host}\\${f.export}\\${b.path ?? ""}`
            : `${f.host}:${f.export}/${b.path ?? ""}`,
        path: b.path ?? "",
        dataset_id: b.dataset_id,
        owners_sid: b.owners_sid,
        baseline: { at: null, files: null, bytes: null },
        last_quick_scan_at: null,
        last_full_scan_at: null,
        last_reconcile_at: null,
        last_event_at: null,
        created_at: iso(now),
        entity_version: 1,
        counts: {
          ...base.counts,
          present: 0,
          copied: 0,
          verified: 0,
          pending: 0,
          gone: 0,
          bytes: 0,
          bytes_verified: 0,
          problems_30d: 0,
        },
        filer: f,
      };
      state.annexes.push(a);
      state.files.set(a.annex_id, []);
      state.events.set(a.annex_id, []);
      return json(summary(a), { status: 201, headers: { ETag: '"1"' } });
    }),
    http.get("*/v1/annexes/:id", ({ params }) => {
      const a = find(String(params.id));
      if (!a) return problem(404, "not_found", "no such annex");
      // As the service: purging (and a refused copy's buttons) is an ArchiveAdmin's.
      return json({ ...a, can_purge: admin() }, { headers: { ETag: `"${a.entity_version}"` } });
    }),
    http.patch("*/v1/annexes/:id", async ({ params, request }) => {
      const a = find(String(params.id));
      if (!a) return problem(404, "not_found", "no such annex");
      if (!request.headers.get("If-Match"))
        return problem(428, "precondition_required", "If-Match is required");
      if (request.headers.get("If-Match") !== `"${a.entity_version}"`)
        return problem(412, "precondition_failed", "the annex changed");
      const b = (await request.json()) as components["schemas"]["AnnexPatch"];
      if (b.state) a.state = b.state;
      if (b.settings) a.settings = { ...a.settings, ...b.settings };
      a.entity_version += 1;
      return json(summary(a), { headers: { ETag: `"${a.entity_version}"` } });
    }),
    http.get("*/v1/annexes/:id/events", ({ params, request }) => {
      const kinds = new URL(request.url).searchParams.get("kind")?.split(",");
      const evs = (state.events.get(String(params.id)) ?? []).filter(
        (e) => !kinds || kinds.includes(e.kind),
      );
      return json({ items: [...evs].reverse(), next_cursor: null });
    }),
    http.get("*/v1/annexes/:id/files", ({ params, request }) => {
      const url = new URL(request.url);
      const st = url.searchParams.get("state");
      const prefix = url.searchParams.get("path") ?? "";
      const items = (state.files.get(String(params.id)) ?? []).filter(
        (f) =>
          f.path.startsWith(prefix) &&
          (!st ||
            (st === "present" && !f.gone_since) ||
            (st === "gone" && f.gone_since && !f.purged_at) ||
            (st === "purged" && f.purged_at)),
      );
      return json({ items, next_cursor: null });
    }),
    http.get("*/v1/annexes/:id/timeline", ({ params, request }) => {
      const days = Number(new URL(request.url).searchParams.get("days") ?? "30");
      const now = Date.now();
      const evs = state.events.get(String(params.id)) ?? [];
      const out = Array.from({ length: days }, (_, i) => {
        const date = new Date(now - (days - 1 - i) * DAY).toISOString().slice(0, 10);
        const of = (k: string) =>
          evs.filter((e) => e.kind === k && e.at.slice(0, 10) === date).length;
        return {
          date,
          arrived: of("arrived"),
          left: of("left"),
          changed: of("changed"),
          copied: of("copied"),
          verified: of("verified"),
          bytes_copied: 0,
        };
      });
      const a = find(String(params.id));
      return json({ annex_id: String(params.id), baseline: a?.baseline ?? {}, days: out });
    }),
    http.get("*/v1/annexes/:id/problems", ({ params }) => {
      const evs = (state.events.get(String(params.id)) ?? []).filter((e) =>
        ["skipped", "copy_failed", "problem", "drift"].includes(e.kind),
      );
      const drift =
        String(params.id) === "7"
          ? [
              {
                drift_id: "61",
                kind: "changed_no_event",
                path: "dataset/notes.txt",
                expected: { size: 4000 },
                observed: { size: 4096, resolved_by: "changed" },
                detected_at: iso(Date.now() - 3 * DAY),
                last_seen_at: iso(Date.now() - 3 * DAY),
                resolved: true,
              },
            ]
          : [];
      // Drift names NAS paths: rows for ArchiveAdmins, counts for others.
      const counts: Record<string, number> = {};
      for (const d of drift) if (!d.resolved) counts[d.kind] = (counts[d.kind] ?? 0) + 1;
      return json({
        annex_id: String(params.id),
        drift_counts: counts,
        drift: admin() ? drift : [],
        events: evs,
        copies_refused: state.refused.get(String(params.id)) ?? [],
        copies_refused_total:
          state.refusedTotal.get(String(params.id)) ??
          (state.refused.get(String(params.id)) ?? []).length,
        read_set: state.readSets.get(String(params.id)) ?? {
          ready: true,
          filer_answered: true,
          read_at: iso(Date.now() - DAY),
          folders: 12,
          unreadable: 0,
        },
      });
    }),
    http.get("*/v1/annexes/:id/rulesets", ({ params }) => {
      const list = state.rulesets.get(String(params.id)) ?? [];
      return json({
        items: [...list].reverse().map(({ body: _b, ...r }) => r),
        next_cursor: null,
      });
    }),
    http.get("*/v1/annexes/:id/rulesets/:v", ({ params }) => {
      const rs = (state.rulesets.get(String(params.id)) ?? []).find(
        (r) => String(r.version) === String(params.v),
      );
      if (!rs) return problem(404, "not_found", "no such rule set");
      const run = rs.dry_run_id ? state.dryRuns.get(rs.dry_run_id) : undefined;
      const running = (state.rulesets.get(String(params.id)) ?? []).find(
        (r) => r.state === "active",
      );
      return json({
        ...rs,
        checks: [],
        dry_run: run ?? null,
        running_version: running?.version ?? null,
        running_body: running && running.version !== rs.version ? running.body : null,
      });
    }),
    http.post("*/v1/annexes/:id/rulesets", async ({ params, request }) => {
      const id = String(params.id);
      const a = find(id);
      if (!a) return problem(404, "not_found", "no such annex");
      const b = (await request.json()) as { body: RuleBody; rationale?: string };
      const rules = b.body.rules as MockRule[];
      const last = rules.filter((r) => r.trigger === "arrived").at(-1);
      if (last && last.conditions && Object.keys(last.conditions).length) {
        return HttpResponse.json(
          {
            type: "urn:omnifile:error:rules_check_failed",
            title: "Rule set check failed",
            status: 422,
            code: "rules_check_failed",
            retryable: false,
            detail: "the rule set can't be saved as it is",
            rule_checks: [
              { rule: last.id ?? "", check: "missing_default", message: "needs a last rule" },
            ],
          },
          { status: 422, headers: { "Content-Type": "application/problem+json" } },
        );
      }
      const list = state.rulesets.get(id) ?? [];
      const version = (list.at(-1)?.version ?? 0) + 1;
      state.next += 1;
      const runId = String(state.next);
      state.dryRuns.set(runId, {
        run_id: runId,
        annex_id: id,
        ruleset_version: version,
        state: "done",
        requested_at: iso(Date.now()),
        done_at: iso(Date.now()),
        error: null,
        report: report(b.body, state.files.get(id) ?? []),
      });
      const removes = removing(b.body);
      const rs = {
        version,
        state: "proposed",
        mode: "custom",
        removes,
        needs_archive_admin: removes,
        owner_identity_id: "12",
        owner_name: "Owen Owner",
        proposed_via: "portal",
        proposed_by_app: null,
        rationale: b.rationale ?? null,
        created_at: iso(Date.now()),
        decided_at: null,
        decided_by_identity_id: null,
        decision_note: null,
        dry_run_id: runId,
        body: b.body,
      };
      state.rulesets.set(id, [...list, rs]);
      a.pending_proposals = (a.pending_proposals ?? 0) + 1;
      // Every proposal moves the annex's ETag, as the service does.
      a.entity_version += 1;
      return json(rs, { status: 201 });
    }),
    http.post("*/v1/annexes/:id/removals/confirm", ({ params, request }) => {
      const a = find(String(params.id));
      if (!a) return problem(404, "not_found", "no such annex");
      if (!admin()) return problem(403, "role_required", "needs the app role ArchiveAdmin");
      if (request.headers.get("If-Match") !== `"${a.entity_version}"`)
        return problem(412, "precondition_failed", "the annex changed");
      if (a.removals) {
        a.removals.confirmed_version = a.ruleset_version;
        a.removals.enabled = !a.removals.paused;
      }
      a.entity_version += 1;
      return json(summary(a), { headers: { ETag: `"${a.entity_version}"` } });
    }),
    http.get("*/v1/annexes/:id/removals", () =>
      json({
        items: [],
        next_cursor: null,
        live: false,
        why_not: "not_confirmed",
        // As the service: ArchiveAdmins and apps only.
        would_remove: admin()
          ? {
              files: 2,
              bytes: 3 * 1024 ** 3,
              samples: ["surveys/2025/line_0001.segy", "surveys/2025/line_0002.segy"],
            }
          : null,
      }),
    ),
    http.post("*/v1/annexes/:id/rulesets/:v/:decision", ({ params, request }) => {
      const id = String(params.id);
      const a = find(id);
      if (!a) return problem(404, "not_found", "no such annex");
      if (request.headers.get("If-Match") !== `"${a.entity_version}"`)
        return problem(412, "precondition_failed", "the annex changed");
      const list = state.rulesets.get(id) ?? [];
      const rs = list.find((r) => String(r.version) === String(params.v));
      if (!rs || rs.state !== "proposed")
        return problem(409, "version_conflict", "not waiting for a decision");
      if (params.decision === "approve") {
        if (rs.needs_archive_admin && !admin())
          return problem(403, "role_required", "needs the app role ArchiveAdmin");
        for (const r of list) if (r.state === "active") r.state = "superseded";
        rs.state = "active";
        a.ruleset_version = rs.version;
        a.mode = rs.mode;
        if (rs.removes && a.removals) a.removals.dry_run_until = iso(Date.now() + 7 * DAY);
      } else {
        rs.state = "rejected";
      }
      rs.decided_at = iso(Date.now());
      a.pending_proposals = list.filter((r) => r.state === "proposed").length;
      a.entity_version += 1;
      return json(summary(a), { headers: { ETag: `"${a.entity_version}"` } });
    }),
    http.post("*/v1/annexes/:id/dry-runs", async ({ params, request }) => {
      // As the service: counts over the NAS listing are an ArchiveAdmin's.
      if (!admin()) return problem(403, "role_required", "previews are an ArchiveAdmin's");
      const id = String(params.id);
      const b = (await request.json()) as { body: RuleBody | null };
      const body = b.body ?? COPY_BODY;
      state.next += 1;
      const run: DryRun = {
        run_id: String(state.next),
        annex_id: id,
        ruleset_version: null,
        state: "requested",
        requested_at: iso(Date.now()),
        done_at: null,
        error: null,
        report: null,
      };
      // The edge answers at once in the mock.
      state.dryRuns.set(run.run_id, {
        ...run,
        state: "done",
        done_at: iso(Date.now()),
        report: report(body, state.files.get(id) ?? []),
      });
      return json({ ...run, checks: [] }, { status: 202 });
    }),
    http.get("*/v1/annexes/:id/dry-runs/:run", ({ params }) => {
      const run = state.dryRuns.get(String(params.run));
      if (!run) return problem(404, "not_found", "no such dry run");
      const out = admin() ? run : { ...run, report: null, report_hidden: run.report !== null };
      return json(out);
    }),
    http.get("*/v1/files/:fid/return-targets", ({ params }) => {
      for (const [annexId, files] of state.files) {
        const f = files.find((x) => x.file_id === String(params.fid));
        if (f) return json(returnTargets(annexId, f));
      }
      return problem(404, "not_found", "no such file");
    }),
    http.post("*/v1/placements", async ({ request }) => {
      const b = (await request.json()) as {
        file_id: string;
        place_at: unknown;
        priority?: string;
        note?: string;
      };
      for (const [annexId, files] of state.files) {
        const f = files.find((x) => x.file_id === b.file_id);
        if (!f) continue;
        const t = returnTargets(annexId, f).items.find((x) =>
          b.place_at === "default"
            ? x.default
            : JSON.stringify(x.location) === JSON.stringify(b.place_at),
        );
        if (!t?.allowed || !t.target)
          return problem(403, "target_not_allowed", "you can't have it placed there");
        if (f.tier === "archive" && !b.priority)
          return problem(400, "invalid_request", "priority: the file is in Archive");
        state.next += 1;
        const p: Placement = {
          placement_id: String(state.next),
          annex_id: annexId,
          file_id: f.file_id,
          version_id: f.version_id ?? "1",
          recall_id: f.tier === "archive" ? String(state.next + 1000) : null,
          requester_identity_id: "11",
          request_via: "portal",
          note: b.note ?? null,
          rule: "back",
          ruleset_version: 1,
          target: { ...t.target, display: t.display ?? "" },
          display: t.display ?? null,
          chosen: b.place_at === "default" ? null : t.location,
          on_conflict: t.on_conflict,
          hold_days: t.hold_days,
          state: f.tier === "archive" ? "rehydrating" : "ready",
          outcome: null,
          fail_reason: null,
          fail_detail: null,
          final_path: null,
          requested_at: iso(Date.now()),
          updated_at: iso(Date.now()),
          done_at: null,
          joined: false,
        };
        state.placements.set(p.placement_id, p);
        return json(p, { status: 201 });
      }
      return problem(404, "not_found", "no such file");
    }),
    http.get("*/v1/placements/:pid", ({ params }) => {
      const p = state.placements.get(String(params.pid));
      return p ? json(p) : problem(404, "not_found", "no such placement");
    }),
    http.post("*/v1/annexes/:id/return-preview", async ({ params, request }) => {
      if (!admin()) return problem(403, "role_required", "previews are an ArchiveAdmin's");
      const b = (await request.json()) as { body: RuleBody | null };
      const body = b.body ?? COPY_BODY;
      const removed = (state.files.get(String(params.id)) ?? []).filter(
        (f) => f.nas_state === "gone" || f.nas_state === "removed",
      );
      const back = (body.rules as MockRule[]).filter((r) => r.trigger === "recall_completed");
      const bytes = removed.reduce((n, f) => n + f.size, 0);
      return json({
        files: removed.length,
        truncated: false,
        placed_nowhere: back.length ? { files: 0, bytes: 0 } : { files: removed.length, bytes },
        rules: back.map((r, i) => ({
          id: r.id ?? `back.${i}`,
          files: i === 0 ? removed.length : 0,
          bytes: i === 0 ? bytes : 0,
          may_take: 0,
          targets:
            i === 0
              ? [
                  {
                    location: (r.actions ?? []).find((a) => "place_at" in a)?.place_at ?? {
                      kind: "nas_folder",
                      origin: true,
                    },
                    files: removed.length,
                    bytes,
                    names_taken: 1,
                    free_bytes: 63 * 1024 ** 4,
                  },
                ]
              : [],
        })),
        checks: [],
      });
    }),
    ...(["copy-stop", "copy-resume", "copy-refused-ack"] as const).map((action) =>
      http.post(`*/v1/annexes/:id/files/:fid/${action}`, ({ params }) => {
        if (!admin()) return problem(403, "role_required", "ArchiveAdmin only.");
        const r = (state.refused.get(String(params.id)) ?? []).find(
          (x) => x.file_id === String(params.fid),
        );
        const f = (state.files.get(String(params.id)) ?? []).find(
          (x) => x.file_id === String(params.fid),
        );
        if (!r || !f) return problem(409, "copy_not_refused", "no read-back refused it");
        const mark = { by: "user:admin", at: iso(Date.now()) };
        if (action === "copy-stop") r.copy_stopped = r.copy_stopped ?? mark;
        if (action === "copy-resume") r.copy_stopped = null;
        if (action === "copy-refused-ack") r.copy_refused_ack = r.copy_refused_ack ?? mark;
        // As the service: at the bound or stopped, and not acknowledged.
        r.alerting = (r.at_bound || r.copy_stopped !== null) && r.copy_refused_ack === null;
        return json({ ...f, ...r });
      }),
    ),
    http.post("*/v1/annexes/:id/files/:fid/purge", ({ params }) => {
      if (!admin()) return problem(403, "role_required", "ArchiveAdmin only.");
      const f = (state.files.get(String(params.id)) ?? []).find(
        (x) => x.file_id === String(params.fid),
      );
      if (!f) return problem(404, "not_found", "no such annex file");
      if (!f.gone_since) return problem(409, "file_present", "the file is still on the NAS");
      f.purge_requested_at = iso(Date.now());
      return json(f, { status: 202 });
    }),
    http.get("*/v1/settings/annex-defaults", () =>
      json(
        { ...state.defaults, updated_at: iso(Date.now()), updated_by: null },
        { headers: { ETag: `"${state.defaultsVersion}"` } },
      ),
    ),
    http.put("*/v1/settings/annex-defaults", async ({ request }) => {
      if (!admin()) return problem(403, "role_required", "ArchiveAdmin only.");
      if (request.headers.get("If-Match") !== `"${state.defaultsVersion}"`)
        return problem(412, "precondition_failed", "the defaults changed");
      const b = (await request.json()) as components["schemas"]["AnnexSettings"];
      state.defaults = { ...state.defaults, ...b };
      state.defaultsVersion += 1;
      return json(
        { ...state.defaults, updated_at: iso(Date.now()), updated_by: "user:1" },
        { headers: { ETag: `"${state.defaultsVersion}"` } },
      );
    }),
  ];
}
