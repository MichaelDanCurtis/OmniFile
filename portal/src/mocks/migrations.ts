import { http, type HttpHandler } from "msw";
import type { components } from "../api/schema.gen";
import type { Dataset, ProblemCode } from "../api/types";
import { createConsoleMock, type ConsoleMock } from "./console";
import { example } from "./examples.gen";

/**
 * The migrations and sources part of the mock (portal-api.md 5.10, 5.17, 5.18):
 * S3 source backends, sources that catalogue themselves, and migrations that
 * walk the lifecycle. A source or migration made in the mock moves one step
 * each time it is read on its own (GET /v1/sources/{id}, /v1/migrations/{id}),
 * so `dev:mock` shows it progress and tests stay deterministic.
 */

type S = components["schemas"];
type Source = S["Source"];
type Migration = S["Migration"];
type SourceBackend = S["SourceBackend"];
type Tombstone = S["Tombstone"];

export interface MigrationMock {
  sourceBackends: SourceBackend[];
  sources: Source[];
  migrations: Migration[];
  /** Rows of each migration's cutover report, with whether the admin can read them. */
  tombstones: Map<string, (Tombstone & { readable: boolean })[]>;
  /** Which objects the worker lists as stuck, by migration and kind. */
  stuckKeys: Map<string, { stranded: string[]; no_checksum: string[] }>;
  /** Ids that advance when read (made in this mock session). */
  simulated: Set<string>;
  /** The console: plans and Storage Mover agents (mocks/console.ts). */
  console: ConsoleMock;
}

const SID = /^S-1-5-21(-\d+){3,}$/;

export function createMigrationMock(datasets: Dataset[]): MigrationMock {
  const first = datasets[0];
  // The first dataset is migrating from the S3 backend below (its sources).
  if (first) first.migration_state = "migrating";
  const ds = first?.dataset_id ?? "1";
  const home = first?.backend_id ?? "1";
  const owners = "S-1-5-21-3623811015-3361044348-30300820-1201";
  const readers = "S-1-5-21-3623811015-3361044348-30300820-1203";
  const sourceBackends: SourceBackend[] = [
    { ...example.SourceBackend },
    {
      backend_id: "8",
      name: "aws-eu-west-1-any",
      kind: "s3",
      region: "eu-west-1",
      private_network: true,
      buckets: null,
      metadata_tables: [],
    },
  ];
  const sources: Source[] = [
    {
      ...example.Source,
      source_id: "9",
      dataset_id: ds,
      prefix: "raw/2021/",
      path: "seismic/raw2021",
      state: "seeded",
      inventory: null,
      groups: { owners, writers: null, readers },
      counts: {
        objects_seen: 4200,
        files_created: 4200,
        dirs_created: 31,
        already_present: 0,
        skipped: 0,
      },
      seeded_at: "2026-10-20T11:00:00.000Z",
      created_at: "2026-10-20T10:00:00.000Z",
      updated_at: "2026-10-20T11:00:00.000Z",
    },
    {
      ...example.Source,
      source_id: "4",
      dataset_id: ds,
      prefix: "raw/2020/",
      path: "seismic/raw2020",
      state: "failed",
      last_error: "the inventory is older than the bucket's event queue keeps",
    },
    {
      ...example.Source,
      dataset_id: ds,
      state: "seeded",
      seeded_at: "2026-10-14T10:00:00.000Z",
    },
  ];
  const base = {
    ...example.Migration,
    dataset_id: ds,
    to_backend_id: home,
    created_by: "app:migration-partner",
  };
  const migrations: Migration[] = [
    {
      ...base,
      migration_id: "7",
      plan: { plan_id: "3", seq: 2 },
      prefix: "raw/2019/batch-b/",
      cutover: "auto",
      copier_kind: "storage_mover",
      state: "copying",
      files: { total: 15000, verified: 9100, failed: 2, excluded: 0 },
      copier: {
        job_ref: "omnifile-migration-7",
        last_run: {
          run_ref: "run-4",
          state: "running",
          counters: { scanned: 15000, transferred: 9300, failed: 2 },
          started_at: "2026-10-16T08:00:00.000Z",
        },
        runs_reported: 0,
      },
      verified_at: null,
      source_deletes_pending: 0,
      created_by: "user:1",
    },
    { ...base },
    {
      ...base,
      migration_id: "6",
      plan: { plan_id: "3", seq: 1 },
      prefix: "raw/2019/batch-0/",
      cutover: "auto",
      copier_kind: "storage_mover",
      state: "cut_over",
      files: { total: 900, verified: 900, failed: 0, excluded: 0 },
      copier: { job_ref: "omnifile-migration-6", last_run: null, runs_reported: 0 },
      cut_over_at: "2026-10-14T18:00:00.000Z",
      source_deletes_pending: 0,
    },
  ];
  const row = (n: number, readable: boolean): Tombstone & { readable: boolean } => ({
    file_id: String(41 + n),
    path: `/seismic/raw/2019/line_${1042 + n}.segy`,
    source: {
      backend_id: "7",
      container: "acme-seismic",
      key: `raw/2019/line_${1042 + n}.segy`,
      backend_version_id: n % 2 ? `v${n}` : null,
      etag: '"9b2cf535f27731c974343645a3985328"',
      verify_etag_before_delete: n % 2 === 0,
    },
    deleted_at: "2026-10-15T09:12:00.000Z",
    deleted_by: {
      kind: "edge",
      edge_id: "1",
      identity_id: n === 0 ? "11" : null,
      proof: n === 0 ? "proven" : "unknown",
    },
    readable,
  });
  return {
    sourceBackends,
    sources,
    migrations,
    tombstones: new Map([["5", [row(0, true), row(1, false), row(2, true), row(3, false)]]]),
    stuckKeys: new Map(),
    simulated: new Set(),
    console: createConsoleMock(ds),
  };
}

export interface MockContext {
  state: () => MigrationMock;
  datasets: () => Dataset[];
  isAdmin: () => boolean;
  needsReauth: () => Response | null;
  problem: (
    status: number,
    code: ProblemCode,
    detail: string,
    extra?: Record<string, unknown>,
  ) => Response;
  json: (body: unknown, init?: ResponseInit) => Response;
  newId: () => string;
  identityId: () => string;
  /** The caller has an on-premises SID (an AD identity). */
  hasSid: () => boolean;
  audit: (action: string, extra?: Record<string, unknown>) => void;
}

const nowIso = () => new Date().toISOString();

/** One step of a simulated source: registered -> seeding -> seeded. */
function advanceSource(s: Source) {
  if (s.state === "registered") {
    s.state = "seeding";
    s.snapshot_at = nowIso();
    s.counts = { ...s.counts, objects_seen: 1800, files_created: 1790, dirs_created: 12 };
  } else if (s.state === "seeding") {
    s.state = "seeded";
    s.seeded_at = nowIso();
    s.counts = {
      objects_seen: 3600,
      files_created: 3588,
      dirs_created: 24,
      already_present: 0,
      skipped: 12,
    };
  }
  s.updated_at = nowIso();
}

/** One step of a simulated migration, along the lifecycle. */
function advanceMigration(m: Migration) {
  const total = m.files.total ?? 3588;
  switch (m.state) {
    case "pending":
      m.state = "copying";
      m.files = { ...m.files, total };
      if (m.copier_kind === "storage_mover") {
        m.copier = {
          job_ref: `omnifile-migration-${m.migration_id}`,
          last_run: {
            run_ref: "run-1",
            state: "running",
            counters: { scanned: total, transferred: Math.round(total / 2) },
            started_at: nowIso(),
          },
          runs_reported: 0,
        };
      }
      break;
    case "copying":
      m.state = "verifying";
      m.files = { ...m.files, verified: Math.round(total / 2) };
      if (m.copier.last_run) {
        m.copier = {
          ...m.copier,
          last_run: {
            ...m.copier.last_run,
            state: "succeeded",
            counters: { scanned: total, transferred: total },
          },
        };
      }
      break;
    case "verifying":
      m.state = "verified";
      m.files = { ...m.files, verified: total };
      m.verified_at = nowIso();
      m.journal_checked_through = nowIso();
      break;
    case "verified":
      if (m.cutover === "auto" || m.cutover_requested_at) {
        m.state = "cut_over";
        m.cut_over_at = nowIso();
      }
      break;
  }
  m.updated_at = nowIso();
}

export function migrationHandlers(c: MockContext): HttpHandler[] {
  const adminOnly = () =>
    c.isAdmin() ? null : c.problem(403, "role_required", "ArchiveAdmin only.");
  const bad = (field: string, message: string) =>
    c.problem(400, "invalid_request", `${field}: ${message}`, { errors: [{ field, message }] });
  const conflict = (code: ProblemCode, detail: string) => c.problem(409, code, detail);
  const findMigration = (id: string) => c.state().migrations.find((m) => m.migration_id === id);
  /** A source as the API answers it: with its migrations that aren't cancelled. */
  const view = (s: Source): Source => ({
    ...s,
    active_migrations: c
      .state()
      .migrations.filter((m) => m.source_id === s.source_id && m.state !== "cancelled").length,
  });

  return [
    http.get("*/v1/source-backends", () => {
      const refused = adminOnly();
      if (refused) return refused;
      return c.json({ backends: c.state().sourceBackends });
    }),

    http.get("*/v1/sources", ({ request }) => {
      const refused = adminOnly();
      if (refused) return refused;
      const url = new URL(request.url);
      const ds = url.searchParams.get("dataset_id");
      const items = c
        .state()
        .sources.filter((s) => !ds || s.dataset_id === ds)
        .sort((a, b) => Number(b.source_id) - Number(a.source_id))
        .map(view);
      return c.json({ items, next_cursor: null });
    }),

    http.get("*/v1/sources/:id", ({ params }) => {
      const refused = adminOnly();
      if (refused) return refused;
      const s = c.state().sources.find((x) => x.source_id === String(params.id));
      if (!s) return c.problem(404, "not_found", "No such source.");
      if (c.state().simulated.has(`source:${s.source_id}`)) advanceSource(s);
      return c.json(view(s));
    }),

    http.post("*/v1/sources", async ({ request }) => {
      const refused = adminOnly();
      if (refused) return refused;
      const b = (await request.json()) as Partial<S["SourceCreate"]>;
      const dataset = c.datasets().find((d) => d.dataset_id === b.dataset_id);
      if (!dataset) return bad("dataset_id", "is not a dataset");
      const backend = c.state().sourceBackends.find((x) => x.backend_id === b.backend_id);
      if (!backend) return bad("backend_id", "is not a backend an ArchiveAdmin configured");
      const container = b.container ?? "";
      if (!container) return bad("container", "is required");
      if (backend.buckets && !backend.buckets.includes(container)) {
        return bad("body", `bucket '${container}' is not one of backend ${backend.backend_id}'s`);
      }
      const prefix = b.prefix ?? "";
      if (prefix && !prefix.endsWith("/")) return bad("body", "prefix must be empty or end in '/'");
      const path = b.path ?? "";
      if (path.startsWith("/") || path.endsWith("/")) {
        return bad("body", "path must not start or end with '/'");
      }
      const groups = b.groups;
      if (!groups?.owners) return bad("groups", "owners is required");
      for (const k of ["owners", "writers", "readers"] as const) {
        const v = groups[k];
        if (v && !SID.test(v)) return bad(`groups.${k}`, "is not a SID");
      }
      if (dataset.migration_state !== "steady" && dataset.migration_state !== "migrating") {
        return conflict(
          "source_conflict",
          `dataset ${dataset.dataset_id} is ${dataset.migration_state}`,
        );
      }
      const from = c
        .state()
        .sources.find((s) => s.dataset_id === dataset.dataset_id && s.kind === "source");
      if (from && from.backend_id !== backend.backend_id) {
        return conflict(
          "source_conflict",
          `dataset ${dataset.dataset_id} is migrating from backend ${from.backend_id}; it can't` +
            ` also take sources from backend ${backend.backend_id}`,
        );
      }
      const clash = c
        .state()
        .sources.find(
          (s) =>
            s.backend_id === backend.backend_id &&
            s.container === container &&
            (s.prefix.startsWith(prefix) || prefix.startsWith(s.prefix)),
        );
      if (clash) {
        return conflict(
          "source_conflict",
          `prefix '${prefix}' overlaps source ${clash.source_id} ('${clash.prefix}')`,
        );
      }
      dataset.migration_state = "migrating";
      const at = nowIso();
      const source: Source = {
        source_id: c.newId(),
        kind: "source",
        dataset_id: dataset.dataset_id,
        path,
        backend_id: backend.backend_id,
        container,
        prefix,
        inventory:
          b.inventory ??
          (backend.metadata_tables.includes(container)
            ? {
                kind: "s3_metadata",
                table_bucket_arn: "arn:aws:s3tables:us-east-1:123456789012:bucket/aws-s3",
              }
            : null),
        groups: {
          owners: groups.owners,
          writers: groups.writers ?? null,
          readers: groups.readers ?? null,
        },
        state: "registered",
        snapshot_at: null,
        counts: {
          objects_seen: 0,
          files_created: 0,
          dirs_created: 0,
          already_present: 0,
          skipped: 0,
        },
        last_error: null,
        active_migrations: 0,
        created_at: at,
        updated_at: at,
        seeded_at: null,
      };
      c.state().sources.push(source);
      c.state().simulated.add(`source:${source.source_id}`);
      c.audit("source.register", { dataset_id: dataset.dataset_id });
      return c.json(source, { status: 201 });
    }),

    http.post("*/v1/sources/:id/retry", ({ params }) => {
      const refused = adminOnly();
      if (refused) return refused;
      const s = c.state().sources.find((x) => x.source_id === String(params.id));
      if (!s) return c.problem(404, "not_found", "No such source.");
      if (s.state !== "failed") return conflict("source_conflict", `the source is ${s.state}`);
      s.state = "seeding";
      s.last_error = null;
      c.state().simulated.add(`source:${s.source_id}`);
      return c.json(s);
    }),

    http.get("*/v1/migrations", ({ request }) => {
      const refused = adminOnly();
      if (refused) return refused;
      const ds = new URL(request.url).searchParams.get("dataset_id");
      const items = c
        .state()
        .migrations.filter((m) => !ds || m.dataset_id === ds)
        .sort((a, b) => Number(b.migration_id) - Number(a.migration_id));
      return c.json({ items, next_cursor: null });
    }),

    http.get("*/v1/migrations/:id", ({ params }) => {
      const refused = adminOnly();
      if (refused) return refused;
      const m = findMigration(String(params.id));
      if (!m) return c.problem(404, "not_found", "No such migration.");
      if (c.state().simulated.has(`migration:${m.migration_id}`)) advanceMigration(m);
      return c.json(m);
    }),

    http.post("*/v1/migrations", async ({ request }) => {
      const refused = adminOnly();
      if (refused) return refused;
      const b = (await request.json()) as Partial<S["MigrationCreate"]>;
      const dataset = c.datasets().find((d) => d.dataset_id === b.dataset_id);
      if (!dataset) return bad("dataset_id", "is not a dataset");
      const prefix = b.prefix ?? "";
      if (b.mode && b.mode !== "serve") return bad("mode", "must be serve (hand_off is reserved)");
      if (dataset.migration_state !== "migrating") {
        return conflict(
          "migration_conflict",
          `dataset ${dataset.dataset_id} is ${dataset.migration_state}`,
        );
      }
      const source = c
        .state()
        .sources.filter(
          (s) =>
            s.dataset_id === dataset.dataset_id &&
            s.kind === "source" &&
            prefix.startsWith(s.prefix),
        )
        .sort((a, z) => z.prefix.length - a.prefix.length)[0];
      if (!source) {
        return bad("body", `'${prefix}' is not inside a source of dataset ${dataset.dataset_id}`);
      }
      if (source.state !== "seeded") {
        return conflict("migration_conflict", `source ${source.source_id} is ${source.state}`);
      }
      const clash = c
        .state()
        .migrations.find(
          (m) =>
            m.source_id === source.source_id &&
            m.state !== "cancelled" &&
            (m.prefix.startsWith(prefix) || prefix.startsWith(m.prefix)),
        );
      if (clash) {
        return conflict(
          "migration_conflict",
          `'${prefix}' overlaps migration ${clash.migration_id} ('${clash.prefix}')`,
        );
      }
      const at = nowIso();
      const m: Migration = {
        migration_id: c.newId(),
        dataset_id: dataset.dataset_id,
        source_id: source.source_id,
        prefix,
        from_backend_id: source.backend_id,
        to_backend_id: dataset.backend_id ?? "1",
        to_container: "live/data",
        target_root: `migrated/${source.container}/`,
        mode: "serve",
        cutover: b.cutover ?? "auto",
        copier_kind: b.copier === "external" ? "external" : "storage_mover",
        state: "pending",
        files: { total: null, verified: 0, failed: 0, excluded: 0 },
        copier: { job_ref: null, last_run: null, runs_reported: 0 },
        journal_checked_through: null,
        verified_at: null,
        cutover_requested_at: null,
        cut_over_at: null,
        cancelled_at: null,
        source_deletes_pending: 0,
        last_error: null,
        created_by: `user:${c.identityId()}`,
        plan: null,
        created_at: at,
        updated_at: at,
      };
      c.state().migrations.push(m);
      c.state().simulated.add(`migration:${m.migration_id}`);
      c.audit("migration.start", { dataset_id: dataset.dataset_id });
      return c.json(m, { status: 202 });
    }),

    http.post("*/v1/migrations/:id/cutover", ({ params }) => {
      const refused = adminOnly();
      if (refused) return refused;
      const m = findMigration(String(params.id));
      if (!m) return c.problem(404, "not_found", "No such migration.");
      if (m.plan) {
        return conflict(
          "migration_conflict",
          `this migration belongs to wave ${m.plan.seq} of plan ${m.plan.plan_id}: cut it over, cancel or retry it on the plan`,
        );
      }
      if (m.cutover !== "manual") {
        return conflict(
          "migration_conflict",
          "this migration cuts over by itself (cutover = auto)",
        );
      }
      if (["cut_over", "cancelled", "failed"].includes(m.state)) {
        return conflict("migration_conflict", `the migration is ${m.state}`);
      }
      m.cutover_requested_at ??= nowIso();
      if (m.state === "verified" && !c.state().simulated.has(`migration:${m.migration_id}`)) {
        // A fixture: the worker would flip it within seconds.
        m.state = "cut_over";
        m.cut_over_at = nowIso();
      }
      c.audit("migration.cutover", { dataset_id: m.dataset_id });
      return c.json(m);
    }),

    http.post("*/v1/migrations/:id/cancel", ({ params }) => {
      const refused = adminOnly();
      if (refused) return refused;
      const m = findMigration(String(params.id));
      if (!m) return c.problem(404, "not_found", "No such migration.");
      if (m.plan) {
        return conflict(
          "migration_conflict",
          `this migration belongs to wave ${m.plan.seq} of plan ${m.plan.plan_id}: cut it over, cancel or retry it on the plan`,
        );
      }
      if (m.state === "cut_over" || m.state === "cancelled") {
        return conflict("migration_conflict", `the migration is ${m.state}`);
      }
      m.state = "cancelled";
      m.cancelled_at = nowIso();
      c.state().simulated.delete(`migration:${m.migration_id}`);
      c.audit("migration.cancel", { dataset_id: m.dataset_id });
      return c.json(m);
    }),

    http.post("*/v1/migrations/:id/retry", ({ params }) => {
      const refused = adminOnly();
      if (refused) return refused;
      const m = findMigration(String(params.id));
      if (!m) return c.problem(404, "not_found", "No such migration.");
      if (m.plan) {
        return conflict(
          "migration_conflict",
          `this migration belongs to wave ${m.plan.seq} of plan ${m.plan.plan_id}: cut it over, cancel or retry it on the plan`,
        );
      }
      if (m.state !== "failed") {
        return conflict(
          "migration_conflict",
          `only a failed migration can be retried; this one is ${m.state}`,
        );
      }
      m.state = "pending";
      m.last_error = null;
      c.state().simulated.add(`migration:${m.migration_id}`);
      c.audit("migration.retry", { dataset_id: m.dataset_id });
      return c.json(m);
    }),

    http.post("*/v1/migrations/:id/exclude", async ({ params, request }) => {
      const refused = adminOnly();
      if (refused) return refused;
      const reauth = c.needsReauth();
      if (reauth) return reauth;
      const m = findMigration(String(params.id));
      if (!m) return c.problem(404, "not_found", "No such migration.");
      const b = (await request.json()) as Partial<S["MigrationExclude"]>;
      const keys = b.object_keys ?? [];
      if (keys.length < 1 || keys.length > 1000) {
        return bad("object_keys", "must be 1 to 1,000 object keys");
      }
      if (!b.reason || b.reason.length > 500) return bad("reason", "must be 1 to 500 characters");
      if (m.state === "cut_over" || m.state === "cancelled") {
        return conflict("migration_conflict", `the migration is ${m.state}`);
      }
      // Only live files' objects under the prefix without a verified copy count.
      const ok = new Set(keys.filter((k) => k.startsWith(m.prefix))).size;
      const n = Math.min(ok, Math.max(0, m.files.failed));
      m.files = { ...m.files, excluded: m.files.excluded + n, failed: m.files.failed - n };
      c.audit("migration.exclude", { dataset_id: m.dataset_id });
      return c.json(m);
    }),

    http.get("*/v1/migrations/:id/stuck-copies", ({ params, request }) => {
      const refused = adminOnly();
      if (refused) return refused;
      const m = findMigration(String(params.id));
      if (!m) return c.problem(404, "not_found", "No such migration.");
      const q = new URL(request.url).searchParams;
      const kind = q.get("kind");
      if (kind !== "stranded" && kind !== "no_checksum") {
        return bad("kind", "must be one of no_checksum, stranded");
      }
      const keys = c.state().stuckKeys.get(m.migration_id)?.[kind] ?? [];
      const limit = Number(q.get("limit") ?? 100);
      const start = Number(q.get("cursor") ?? 0);
      const items = c.hasSid() ? keys.slice(start, start + limit) : [];
      c.audit("migration.stuck_copies.read", { dataset_id: m.dataset_id });
      return c.json({
        migration_id: m.migration_id,
        kind,
        count: m.stuck_copies?.[kind] ?? 0,
        counted_at: m.created_at,
        list_limit: 10000,
        items: items.map((object_key) => ({ object_key })),
        next_cursor: c.hasSid() && start + limit < keys.length ? String(start + limit) : null,
      });
    }),

    http.get("*/v1/migrations/:id/tombstones", ({ params }) => {
      const refused = adminOnly();
      if (refused) return refused;
      const m = findMigration(String(params.id));
      if (!m) return c.problem(404, "not_found", "No such migration.");
      const rows = c.state().tombstones.get(m.migration_id) ?? [];
      return c.json({
        migration_id: m.migration_id,
        state: m.state,
        cut_over_at: m.cut_over_at,
        source_deletes_pending: m.source_deletes_pending,
        applied_by_omnifile: false,
        // The full list for an ArchiveAdmin (owner, 2026-11-04); none without an AD identity.
        items: c.hasSid() ? rows.map(({ readable: _r, ...item }) => item) : [],
        next_cursor: null,
      });
    }),
  ];
}
