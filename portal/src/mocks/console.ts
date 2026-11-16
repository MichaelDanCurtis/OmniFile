import { http, type HttpHandler } from "msw";
import type { components } from "../api/schema.gen";
import type { MockContext } from "./migrations";

/**
 * The migrations console in the mock (portal-api.md 5.22): rich on purpose,
 * so `dev:mock` shows every part of the console. Bytes are 48 MB a file; a
 * plan's due waves start as soon as their source is catalogued (the worker's
 * plan sweep, played here on reads).
 */

type S = components["schemas"];
type Plan = S["MigrationPlan"];
type Wave = S["MigrationWave"];
type Migration = S["Migration"];

const HOUR = 3_600_000;
const FILE_BYTES = 48_000_000;
const iso = (t: number) => new Date(t).toISOString();

export interface ConsoleMock {
  plans: Plan[];
  agents: S["MoverAgent"][];
}

export function createConsoleMock(datasetId: string): ConsoleMock {
  const now = Date.now();
  const day = (d: number, h = 22) => {
    const t = new Date(now + d * 24 * HOUR);
    t.setUTCHours(h, 0, 0, 0);
    return t.getTime();
  };
  const wave = (seq: number, extra: Partial<Wave>): Wave => ({
    seq,
    name: `Wave ${seq}`,
    prefixes: [],
    start_at: iso(day(seq - 2)),
    window: { start: "22:00", end: "06:00" },
    blackouts: [],
    after: seq > 1 ? seq - 1 : null,
    cutover: "auto",
    copier: "auto",
    bandwidth_mbps: 800,
    egress_gb_per_day: null,
    state: "planned",
    waiting_reason: null,
    started_at: null,
    verified_at: null,
    cut_over_at: null,
    migrations: [],
    ...extra,
  });
  const byYear: Plan = {
    plan_id: "3",
    dataset_id: datasetId,
    name: "Seismic raw, by year",
    state: "approved",
    owner: { identity_id: "11", display_name: "Avery Chen" },
    proposed_by: "user:11",
    decided_by: "user:11",
    decided_at: iso(now - 5 * 24 * HOUR),
    decision_note: null,
    version: 4,
    created_at: iso(now - 5 * 24 * HOUR),
    updated_at: iso(now - HOUR),
    waves: [
      wave(1, {
        name: "2018 and before",
        prefixes: ["raw/2019/batch-0/"],
        start_at: iso(day(-4)),
        state: "cut_over",
        started_at: iso(day(-4) + 10 * 60_000),
        verified_at: iso(day(-3, 3)),
        cut_over_at: iso(day(-3, 4)),
        migrations: [{ prefix: "raw/2019/batch-0/", migration_id: "6", state: "cut_over" }],
      }),
      wave(2, {
        name: "2019 batch B",
        prefixes: ["raw/2019/batch-b/"],
        start_at: iso(day(-1)),
        state: "started",
        started_at: iso(day(-1) + 5 * 60_000),
        migrations: [{ prefix: "raw/2019/batch-b/", migration_id: "7", state: "copying" }],
      }),
      wave(3, {
        name: "2021",
        prefixes: ["raw/2021/"],
        start_at: iso(day(1)),
        state: "waiting",
        waiting_reason: "waiting for wave 2 to be verified",
        blackouts: [{ from: iso(day(2, 0)), to: iso(day(2, 12)) }],
      }),
      wave(4, {
        name: "2022 onwards",
        prefixes: ["raw/2022/"],
        start_at: iso(day(3)),
        cutover: "manual",
        bandwidth_mbps: null,
        egress_gb_per_day: 2000,
      }),
    ],
  };
  const proposal: Plan = {
    ...byYear,
    plan_id: "4",
    name: "Well logs (from the API)",
    state: "proposed",
    owner: { identity_id: "21", display_name: "Olga Owner" },
    proposed_by: "app:migration-partner",
    decided_by: null,
    decided_at: null,
    version: 1,
    waves: [
      wave(1, { name: "Logs A–M", prefixes: ["logs/a-m/"], after: null, start_at: iso(day(2)) }),
      wave(2, { name: "Logs N–Z", prefixes: ["logs/n-z/"], start_at: iso(day(4)) }),
    ],
  };
  return {
    plans: [proposal, byYear],
    agents: [
      {
        name: "smagent-houston-01",
        status: "Online",
        version: "3.2.1",
        host: "10.40.8.21",
        arc_resource_id:
          "/subscriptions/x/resourceGroups/rg/providers/Microsoft.HybridCompute/machines/houston-mover-01",
        last_status_at: iso(now - 2 * 60_000),
        error: null,
        polled_at: iso(now - 60_000),
        stale: false,
      },
      {
        name: "smagent-calgary-02",
        status: "Offline",
        version: "3.1.0",
        host: "10.52.1.9",
        arc_resource_id: null,
        last_status_at: iso(now - 7 * HOUR),
        error: "The agent hasn't sent a heartbeat for 7 hours.",
        polled_at: iso(now - 60_000),
        stale: false,
      },
    ],
  };
}

function row(c: MockContext, mock: ConsoleMock, m: Migration) {
  const now = Date.now();
  const total = m.files.total ?? 0;
  const left = (total - m.files.verified) * FILE_BYTES;
  const moving = m.state === "copying" || m.state === "verifying";
  const rate = moving ? 180_000_000 : 0;
  // Half-copied migrations still wait on archived objects in this mock.
  const archived = m.state === "copying" && m.files.verified * 2 < total ? 300 : 0;
  const expiring = m.migration_id === "7" ? 120 : 0;
  const ds = c.datasets().find((d) => d.dataset_id === m.dataset_id);
  const next = mock.plans
    .filter((p) => p.dataset_id === m.dataset_id && p.state === "approved")
    .flatMap((p) =>
      p.waves.filter((w) => w.state === "planned" || w.state === "waiting").map((w) => ({ p, w })),
    )
    .sort((a, b) => a.w.start_at.localeCompare(b.w.start_at))[0];
  return {
    ...m,
    dataset_name: ds?.name ?? `dataset ${m.dataset_id}`,
    counted_at: iso(now - 2 * 60_000),
    failing_now: m.state === "cut_over" ? 0 : m.files.failed,
    bytes: { total: total * FILE_BYTES, verified: m.files.verified * FILE_BYTES },
    throughput: rate
      ? {
          bytes_per_s: rate,
          files_per_h: Math.round((rate * 3600) / FILE_BYTES),
          basis: "copies verified in the last hour",
        }
      : null,
    eta:
      left <= 0
        ? { at: null, basis: "nothing left to verify" }
        : archived
          ? {
              at: null,
              basis: `${archived} objects are still in an S3 archive class and must be restored first: no time is promised`,
            }
          : rate
            ? {
                at: iso(now + (left / rate) * 1000),
                basis: `at the last hour's rate (${Math.round((rate * 3600) / 1e9)} GB verified), ${Math.round(left / 1e9)} GB left`,
              }
            : { at: null, basis: "no copy was verified in the last hour" },
    health:
      m.state === "cut_over"
        ? "done"
        : m.state === "cancelled"
          ? "cancelled"
          : m.state === "failed"
            ? "failing"
            : m.files.failed || expiring
              ? "attention"
              : "ok",
    restores_expiring: expiring,
    archived_waiting: archived,
    next_wave: next
      ? {
          plan_id: next.p.plan_id,
          plan_name: next.p.name,
          seq: next.w.seq,
          name: next.w.name,
          start_at: next.w.start_at,
          waiting_reason: next.w.waiting_reason,
        }
      : null,
  };
}

/** The worker's plan sweep, played on reads: due waves of approved plans start once their source is catalogued. */
function tick(c: MockContext, mock: ConsoleMock) {
  const st = c.state();
  const now = Date.now();
  for (const p of mock.plans) {
    if (p.state !== "approved") continue;
    for (const w of p.waves) {
      // Started waves follow their migrations (migrate/plans.py `_follow`).
      const ms = st.migrations.filter(
        (m) => m.plan?.plan_id === p.plan_id && m.plan.seq === w.seq && m.state !== "cancelled",
      );
      if ((w.state === "started" || w.state === "verified") && ms.length) {
        w.migrations = ms.map((m) => ({
          prefix: m.prefix,
          migration_id: m.migration_id,
          state: m.state,
        }));
        if (ms.every((m) => m.state === "cut_over")) {
          w.state = "cut_over";
          w.cut_over_at = iso(now);
        } else if (ms.every((m) => m.state === "verified" || m.state === "cut_over")) {
          if (w.state === "started") {
            w.state = "verified";
            w.verified_at = iso(now);
          }
        }
        continue;
      }
      if (w.state !== "planned" && w.state !== "waiting") continue;
      if (new Date(w.start_at).getTime() > now) continue;
      if (
        w.after &&
        p.waves.find((x) => x.seq === w.after)?.state !== "verified" &&
        p.waves.find((x) => x.seq === w.after)?.state !== "cut_over"
      )
        continue;
      const src = st.sources.find(
        (s) => s.dataset_id === p.dataset_id && w.prefixes.every((x) => x.startsWith(s.prefix)),
      );
      if (!src) continue;
      if (src.state !== "seeded") {
        w.state = "waiting";
        w.waiting_reason = `the source of '${w.prefixes[0] ?? ""}' is still being catalogued (${src.state})`;
        continue;
      }
      w.state = "started";
      w.started_at = iso(now);
      w.waiting_reason = null;
      w.migrations = w.prefixes.map((prefix) => {
        const id = c.newId();
        st.migrations.push({
          migration_id: id,
          dataset_id: p.dataset_id,
          source_id: src.source_id,
          prefix,
          from_backend_id: src.backend_id,
          to_backend_id: "1",
          to_container: "live/data",
          target_root: `migrated/${src.container}/`,
          mode: "serve",
          cutover: "manual",
          copier_kind: w.copier === "external" ? "external" : "storage_mover",
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
          plan: { plan_id: p.plan_id, seq: w.seq },
          created_by: "system:plans",
          created_at: iso(now),
          updated_at: iso(now),
        });
        st.simulated.add(`migration:${id}`);
        return { prefix, migration_id: id, state: "pending" };
      });
    }
  }
}

export function consoleHandlers(c: MockContext, mock: () => ConsoleMock): HttpHandler[] {
  const adminOnly = () =>
    c.isAdmin() ? null : c.problem(403, "role_required", "ArchiveAdmin only.");
  const find = (id: string) => mock().plans.find((p) => p.plan_id === id);
  const bump = (p: Plan) => {
    p.version += 1;
    p.updated_at = iso(Date.now());
  };
  return [
    http.get("*/v1/migration-console", () => {
      const refused = adminOnly();
      if (refused) return refused;
      tick(c, mock());
      const ms = [...c.state().migrations].sort(
        (a, b) => Number(b.migration_id) - Number(a.migration_id),
      );
      const rows = ms.map((m) => row(c, mock(), m));
      const attention: S["AttentionItem"][] = [];
      for (const r of rows) {
        if (r.state === "cut_over" || r.state === "cancelled") continue;
        if (r.failing_now)
          attention.push({
            kind: "verify_failed",
            migration_id: r.migration_id,
            message: `${r.failing_now} copies failed their check and have no verified copy yet`,
            actions: r.copier_kind === "external" ? ["exclude"] : ["rerun", "exclude"],
          });
        if (r.restores_expiring)
          attention.push({
            kind: "restore_expiring",
            migration_id: r.migration_id,
            message: `${r.restores_expiring} restored S3 copies expire within 24 hours with no verified copy`,
            actions: ["restore_again"],
          });
        if (r.last_error && /AccessDenied|private endpoint/i.test(r.last_error))
          attention.push({
            kind: "access",
            migration_id: r.migration_id,
            message: r.last_error,
            actions: ["settings"],
          });
        if (r.state === "failed")
          attention.push({
            kind: "failed",
            migration_id: r.migration_id,
            message: r.last_error ?? "it stopped",
            actions: ["retry"],
          });
      }
      for (const a of mock().agents) {
        if (a.status !== "Online")
          attention.push({
            kind: "agent_offline",
            migration_id: null,
            agent: a.name,
            message: `Storage Mover agent ${a.name} is ${a.status ?? "unknown"}`,
            actions: ["agents"],
          });
      }
      for (const s of c.state().sources) {
        if (s.kind === "source" && (s.state === "registered" || s.state === "seeding")) {
          attention.push({
            kind: "seeding",
            migration_id: null,
            source_id: s.source_id,
            message: `${s.container}/${s.prefix} is being catalogued (${s.counts.objects_seen.toLocaleString()} objects so far)`,
            actions: ["wait"],
          });
        }
      }
      return c.json({ computed_at: iso(Date.now()), migrations: rows, attention });
    }),

    http.get("*/v1/migration-agents", () => {
      const refused = adminOnly();
      if (refused) return refused;
      const jobs = c
        .state()
        .migrations.filter((m) => m.state !== "cut_over" && m.state !== "cancelled")
        .map((m) => ({
          migration_id: m.migration_id,
          dataset_name: c.datasets().find((d) => d.dataset_id === m.dataset_id)?.name ?? "",
          prefix: m.prefix,
          state: m.state,
          copier_kind: m.copier_kind,
          job_ref: m.copier.job_ref,
          run_state: m.copier.last_run?.state ?? null,
          run_counters: m.copier.last_run?.counters ?? {},
          edge: {
            edge_id: "1",
            name: "edge-houston-1",
            status: "ok",
            software_version: "0.9.0",
            last_heartbeat_at: iso(Date.now() - 40_000),
            guard_state: "armed",
          },
          restore_waves_running: m.migration_id === "7" ? 1 : 0,
          checksum_jobs_running: m.migration_id === "7" ? 1 : 0,
        }));
      return c.json({ agents: mock().agents, jobs, polled: mock().agents.length > 0 });
    }),

    http.get("*/v1/migrations/:id/agents", ({ params }) => {
      const refused = adminOnly();
      if (refused) return refused;
      const m = c.state().migrations.find((x) => x.migration_id === String(params.id));
      if (!m) return c.problem(404, "not_found", "No such migration.");
      const r = row(c, mock(), m);
      const now = Date.now();
      return c.json({
        migration_id: m.migration_id,
        copier_kind: m.copier_kind,
        storage_mover:
          m.copier_kind === "external"
            ? null
            : {
                job_ref: m.copier.job_ref,
                last_run: m.copier.last_run,
                agents: mock().agents,
                agentless: true,
              },
        edge: {
          edge_id: "1",
          name: "edge-houston-1",
          status: "ok",
          software_version: "0.9.0",
          last_heartbeat_at: iso(now - 40_000),
          guard_state: "armed",
        },
        restore_waves:
          m.migration_id === "7"
            ? [
                {
                  wave_id: "31",
                  job_id: "omnifile-wave-7-2",
                  state: "running",
                  objects: 1400,
                  restored: 980,
                  open: 420,
                  speed: "bulk",
                  days: 14,
                  requested_at: iso(now - 30 * HOUR),
                  finished_at: null,
                  first_expiry: iso(now + 20 * HOUR),
                  failure: null,
                },
                {
                  wave_id: "30",
                  job_id: "omnifile-wave-7-1",
                  state: "done",
                  objects: 2600,
                  restored: 2600,
                  open: 0,
                  speed: "bulk",
                  days: 14,
                  requested_at: iso(now - 6 * 24 * HOUR),
                  finished_at: iso(now - 4 * 24 * HOUR),
                  first_expiry: iso(now + 8 * 24 * HOUR),
                  failure: null,
                },
              ]
            : [],
        checksum_jobs:
          m.migration_id === "7"
            ? [
                {
                  checksum_job_id: "12",
                  job_id: "omnifile-checksum-7-3",
                  state: "running",
                  objects: 320,
                  found: null,
                  requested_at: iso(now - 2 * HOUR),
                  finished_at: null,
                  failure: null,
                },
              ]
            : [],
        verification: {
          ...m.files,
          bytes: r.bytes,
          journal_checked_through: m.journal_checked_through,
          archived_waiting: r.archived_waiting,
        },
      });
    }),

    http.post("*/v1/migrations/:id/rerun", ({ params }) => {
      const refused = adminOnly();
      if (refused) return refused;
      const m = c.state().migrations.find((x) => x.migration_id === String(params.id));
      if (!m) return c.problem(404, "not_found", "No such migration.");
      if (m.copier_kind === "external")
        return c.problem(
          409,
          "migration_conflict",
          "an external copier copies and restores itself",
        );
      c.audit("migration.rerun", { dataset_id: m.dataset_id });
      return c.json(m);
    }),

    http.post("*/v1/migrations/:id/restore-again", ({ params }) => {
      const refused = adminOnly();
      if (refused) return refused;
      const m = c.state().migrations.find((x) => x.migration_id === String(params.id));
      if (!m) return c.problem(404, "not_found", "No such migration.");
      c.audit("migration.restore_again", { dataset_id: m.dataset_id });
      return c.json(m);
    }),

    http.get("*/v1/migration-plans", () => {
      const refused = adminOnly();
      if (refused) return refused;
      tick(c, mock());
      return c.json({ items: mock().plans, next_cursor: null });
    }),

    http.get("*/v1/migration-plans/split", ({ request }) => {
      const refused = adminOnly();
      if (refused) return refused;
      const url = new URL(request.url);
      const s = c.state().sources.find((x) => x.source_id === url.searchParams.get("source_id"));
      if (!s) return c.problem(404, "not_found", "No such source.");
      const n = Number(url.searchParams.get("waves") ?? "4");
      const by = url.searchParams.get("by") === "count" ? "count" : "bytes";
      const parts = ["2016", "2017", "2018", "2019", "2020", "2021", "2022", "2023"].map(
        (y, i) => ({
          prefix: `${s.prefix}${y}/`,
          files: 400 + ((i * 937) % 1300),
          bytes: (400 + ((i * 937) % 1300)) * FILE_BYTES * (1 + (i % 3)),
        }),
      );
      const key = by === "bytes" ? "bytes" : "files";
      const buckets = Array.from({ length: Math.max(1, Math.min(n, parts.length)) }, () => ({
        prefixes: [] as string[],
        files: 0,
        bytes: 0,
      }));
      for (const p of [...parts].sort((a, b) => b[key] - a[key])) {
        const b = buckets.reduce((x, y) => (y[key] < x[key] ? y : x));
        b.prefixes.push(p.prefix);
        b.files += p.files;
        b.bytes += p.bytes;
      }
      buckets.forEach((b) => b.prefixes.sort());
      return c.json({
        source_id: s.source_id,
        prefix: s.prefix,
        by,
        prefixes: parts,
        loose: { files: 12, bytes: 12 * FILE_BYTES },
        // A folder this admin can't read: counted, never named.
        hidden: { folders: 1, files: 610, bytes: 610 * FILE_BYTES },
        waves: buckets,
      });
    }),

    http.post("*/v1/migration-plans", async ({ request }) => {
      const refused = adminOnly();
      if (refused) return refused;
      const b = (await request.json()) as S["MigrationPlanCreate"];
      if (!b.name || b.waves.length === 0)
        return c.problem(400, "invalid_request", "name and waves are required");
      const now = iso(Date.now());
      const p: Plan = {
        plan_id: c.newId(),
        dataset_id: b.dataset_id,
        name: b.name,
        state: "approved",
        owner: { identity_id: c.identityId(), display_name: "Avery Chen" },
        proposed_by: `user:${c.identityId()}`,
        decided_by: `user:${c.identityId()}`,
        decided_at: now,
        decision_note: null,
        version: 1,
        created_at: now,
        updated_at: now,
        waves: b.waves.map((w, i) => ({
          seq: i + 1,
          name: w.name ?? `Wave ${i + 1}`,
          prefixes: w.prefixes,
          start_at: w.start_at,
          window: w.window ?? null,
          blackouts: w.blackouts ?? [],
          after: w.after ?? null,
          cutover: w.cutover ?? "auto",
          copier: w.copier ?? "auto",
          bandwidth_mbps: w.bandwidth_mbps ?? null,
          egress_gb_per_day: w.egress_gb_per_day ?? null,
          state: "planned",
          waiting_reason: null,
          started_at: null,
          verified_at: null,
          cut_over_at: null,
          migrations: [],
        })),
      };
      mock().plans.unshift(p);
      c.audit("migration_plan.create", { dataset_id: b.dataset_id });
      return c.json(p, { status: 201 });
    }),

    http.get("*/v1/migration-plans/:id", ({ params }) => {
      const refused = adminOnly();
      if (refused) return refused;
      tick(c, mock());
      const p = find(String(params.id));
      return p ? c.json(p) : c.problem(404, "not_found", "No such plan.");
    }),

    ...(["approve", "reject", "cancel"] as const).map((action) =>
      http.post(`*/v1/migration-plans/:id/${action}`, ({ params }) => {
        const refused = adminOnly();
        if (refused) return refused;
        const p = find(String(params.id));
        if (!p) return c.problem(404, "not_found", "No such plan.");
        if (action !== "cancel" && p.state !== "proposed")
          return c.problem(409, "migration_conflict", `the plan is ${p.state}`);
        if (action === "cancel") {
          p.state = "cancelled";
          p.waves.forEach((w) => {
            if (w.state === "planned" || w.state === "waiting") w.state = "cancelled";
          });
        } else {
          p.state = action === "approve" ? "approved" : "rejected";
          p.decided_by = `user:${c.identityId()}`;
          p.decided_at = iso(Date.now());
        }
        bump(p);
        c.audit(`migration_plan.${action}`, { dataset_id: p.dataset_id });
        return c.json(p);
      }),
    ),

    http.patch("*/v1/migration-plans/:id/waves/:seq", async ({ params, request }) => {
      const refused = adminOnly();
      if (refused) return refused;
      const p = find(String(params.id));
      const w = p?.waves.find((x) => x.seq === Number(params.seq));
      if (!p || !w) return c.problem(404, "not_found", "No such wave.");
      const b = (await request.json()) as S["WavePatch"];
      if (b.base_version !== p.version)
        return c.problem(412, "precondition_failed", "The plan changed since you loaded it.");
      if (w.state !== "planned" && w.state !== "waiting" && w.state !== "blocked")
        return c.problem(409, "migration_conflict", `wave ${w.seq} is ${w.state}`);
      if (b.after !== undefined) {
        w.after = b.after;
        if (w.state === "blocked") {
          w.state = "planned";
          w.waiting_reason = null;
        }
      }
      if (b.start_at) {
        const t = new Date(b.start_at).getTime();
        const dep = p.waves.find((x) => x.seq === w.after);
        if (dep && t < new Date(dep.start_at).getTime()) {
          return c.problem(
            400,
            "invalid_request",
            `start_at: wave ${w.seq} waits for wave ${dep.seq}: it can't start before that`,
            { errors: [{ field: "start_at", message: "before the wave it waits for" }] },
          );
        }
        const later = p.waves.find((x) => x.after === w.seq && new Date(x.start_at).getTime() < t);
        if (later)
          return c.problem(
            400,
            "invalid_request",
            `start_at: wave ${later.seq} waits for this one and starts before that: move that one first`,
          );
        w.start_at = new Date(t).toISOString();
      }
      bump(p);
      c.audit("migration_plan.reschedule", { dataset_id: p.dataset_id });
      return c.json(p);
    }),

    ...(["retry", "cancel"] as const).map((action) =>
      http.post(`*/v1/migration-plans/:id/waves/:seq/${action}`, ({ params }) => {
        const refused = adminOnly();
        if (refused) return refused;
        const p = find(String(params.id));
        const w = p?.waves.find((x) => x.seq === Number(params.seq));
        if (!p || !w) return c.problem(404, "not_found", "No such wave.");
        const ms = c
          .state()
          .migrations.filter((m) => m.plan?.plan_id === p.plan_id && m.plan.seq === w.seq);
        const hit = ms.filter((m) =>
          action === "retry"
            ? m.state === "failed"
            : m.state !== "cut_over" && m.state !== "cancelled",
        );
        if (hit.length === 0)
          return c.problem(409, "migration_conflict", `wave ${w.seq} has nothing to ${action}`);
        for (const m of hit) {
          m.state = action === "retry" ? "pending" : "cancelled";
          if (action === "cancel") m.cancelled_at = iso(Date.now());
          else c.state().simulated.add(`migration:${m.migration_id}`);
        }
        if (action === "cancel" && ms.every((m) => m.state === "cancelled")) w.state = "cancelled";
        if (action === "retry" && w.state === "failed") w.state = "started";
        bump(p);
        c.audit(`migration_plan.wave_${action}`, { dataset_id: p.dataset_id });
        return c.json(p);
      }),
    ),

    http.post("*/v1/migration-plans/:id/waves/:seq/cutover", ({ params }) => {
      const refused = adminOnly();
      if (refused) return refused;
      const p = find(String(params.id));
      const w = p?.waves.find((x) => x.seq === Number(params.seq));
      if (!p || !w) return c.problem(404, "not_found", "No such wave.");
      if (w.state !== "verified")
        return c.problem(409, "migration_conflict", `wave ${w.seq} is ${w.state}`);
      // Asks each verified migration's cutover; they flip at the engine's next step.
      for (const m of c.state().migrations) {
        if (m.plan?.plan_id === p.plan_id && m.plan.seq === w.seq && m.state === "verified") {
          m.cutover_requested_at = iso(Date.now());
        }
      }
      if (!w.migrations.length) {
        w.state = "cut_over";
        w.cut_over_at = iso(Date.now());
      }
      bump(p);
      return c.json(p);
    }),
  ];
}
