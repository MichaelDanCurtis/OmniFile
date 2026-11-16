/**
 * Mock of WP18's routes (portal-api.md 5.13, 5.15, 5.24): the live Activity
 * feed, the appliance's images for Add edge, who sees prices and the price
 * editor. Not in the production bundle.
 */
import { delay, http, HttpResponse, type JsonBodyType } from "msw";
import type { components } from "../api/schema.gen";
import type { MockDb, MockFile, Persona } from "./db";

type S = components["schemas"];
type Role = S["PriceDisplay"]["show_to"][number];

const TEST = import.meta.env.MODE === "test";
const json = (body: unknown, init?: ResponseInit) => HttpResponse.json(body as JsonBodyType, init);
const iso = (ms: number) => new Date(ms).toISOString();
const ROLES: Role[] = ["ArchiveAdmin", "DataSteward", "others"];
const PRICES_KEY = "omnifile.mockPrices";

interface Deps {
  db: () => MockDb;
  isAdmin: () => boolean;
  isSteward: () => boolean;
  readableFile: (id: string) => MockFile | null;
  edges: () => S["Edge"][];
  problem: (status: number, code: S["Problem"]["code"], detail: string) => Response;
}

// ---------------------------------------------------------------- who sees prices

interface PriceState {
  show_to: Role[];
  version: number;
  set_by: string | null;
  set_at: string | null;
  overrides: Map<string, { body: S["PriceTable"]; version: number }>;
}

const prices = new WeakMap<MockDb, PriceState>();

function priceState(db: MockDb): PriceState {
  let s = prices.get(db);
  if (!s) {
    let show: Role[] = [...ROLES];
    if (!TEST) {
      try {
        const saved = JSON.parse(localStorage.getItem(PRICES_KEY) ?? "null") as Role[] | null;
        if (Array.isArray(saved)) show = saved.filter((r) => ROLES.includes(r));
      } catch {
        // storage blocked: everyone sees prices
      }
    }
    s = { show_to: show, version: 1, set_by: null, set_at: null, overrides: new Map() };
    prices.set(db, s);
  }
  return s;
}

/** Tests: set who sees prices in this mock database. */
export function setMockPriceDisplay(db: MockDb, roles: Role[]): void {
  priceState(db).show_to = roles;
}

/** As web/prices.py: a user sees prices when any of their roles is listed. */
export function pricesVisible(db: MockDb, persona: Persona = db.persona): boolean {
  const roles = db.users[persona].roles;
  const keys = (["ArchiveAdmin", "DataSteward"] as const).filter((r) => roles.includes(r));
  return (keys.length ? keys : (["others"] as const)).some((k) =>
    priceState(db).show_to.includes(k),
  );
}

const isMoney = (v: unknown): boolean =>
  !!v &&
  typeof v === "object" &&
  !Array.isArray(v) &&
  "amount" in v &&
  "currency" in v &&
  typeof (v as { amount: unknown }).amount === "string" &&
  Object.keys(v).every((k) => ["amount", "currency", "high"].includes(k));

/** The service's scrub: every money object null, or dropped from a list. */
export function scrubMoney(v: unknown): unknown {
  if (isMoney(v)) return null;
  if (Array.isArray(v)) return v.filter((x) => !isMoney(x)).map(scrubMoney);
  if (v && typeof v === "object") {
    return Object.fromEntries(Object.entries(v).map(([k, x]) => [k, scrubMoney(x)]));
  }
  return v;
}

const AZURE_LIST: S["PriceTable"] = {
  id: "azure_blob/westeurope/2026-09-26",
  backend_kind: "azure_blob",
  region: "westeurope",
  currency: "USD",
  as_of: "2026-09-26",
  verify: false,
  classes: {
    Hot: {
      gb_month: "0.0184",
      write_per_10k: "0.05",
      read_per_10k: "0.004",
      retrieval_per_gb: "0",
    },
    Cool: {
      gb_month: "0.01",
      write_per_10k: "0.1",
      read_per_10k: "0.01",
      retrieval_per_gb: "0.01",
      minimum_days: 30,
    },
    Cold: {
      gb_month: "0.0036",
      write_per_10k: "0.18",
      read_per_10k: "0.1",
      retrieval_per_gb: "0.03",
      minimum_days: 90,
    },
    Archive: {
      gb_month: "0.00099",
      write_per_10k: "0.11",
      read_per_10k: "5",
      retrieval_per_gb: "0.022",
      minimum_days: 180,
    },
  },
};

const S3_LIST: S["PriceTable"] = {
  id: "s3/none",
  backend_kind: "s3",
  region: "eu-west-1",
  currency: "USD",
  as_of: "2026-10-06",
  verify: true,
  classes: {},
  notes: ["No built-in prices for s3; an ArchiveAdmin can set them."],
};

function priceTable(db: MockDb, kind: string): S["PriceTable"] {
  const list = kind === "s3" ? S3_LIST : AZURE_LIST;
  const o = priceState(db).overrides.get(kind);
  const listPart = {
    id: list.id,
    region: list.region,
    currency: list.currency,
    as_of: list.as_of,
    classes: list.classes,
  };
  if (!o) return { ...list, overridden: false, etag: '"0"', list: listPart };
  return { ...o.body, overridden: true, etag: `"${o.version}"`, list: listPart };
}

// ---------------------------------------------------------------- the live feed

interface LiveState {
  seq: number;
  polls: number;
  events: S["LiveEvent"][];
}

const lives = new WeakMap<MockDb, LiveState>();

const KINDS = ["upload", "recall", "heartbeat", "tier", "annex", "upload", "migration", "recall"];
const RECALL_STATES = ["requested", "rehydrating", "copying", "downloading", "available"];

function makeEvent(d: Deps, st: LiveState, at: number): S["LiveEvent"] {
  const db = d.db();
  const n = st.seq++;
  const kind = KINDS[n % KINDS.length] ?? "upload";
  const edges = d.edges().filter((e) => e.state === "active");
  const edge = edges[n % Math.max(1, edges.length)];
  const files = [...db.files.values()].filter((f) => f.kind === "file");
  const file = files[(n * 7) % Math.max(1, files.length)];
  const readable = file ? d.readableFile(file.file_id) : null;
  const share = file ? db.shares.find((s) => s.share_id === file.share_id) : undefined;
  const people = Object.values(db.users);
  const person = people[n % people.length];
  const base = {
    id: `mock:${n}`,
    at: iso(at),
    kind,
    state: null as string | null,
    edge_id: edge?.edge_id ?? null,
    edge_name: edge?.name ?? null,
    dataset_id: share?.dataset_id ?? null,
    share_id: share?.share_id ?? null,
    share_name: share?.name ?? null,
    file_id: readable ? readable.file_id : null,
    name: readable ? readable.name : null,
    path: readable ? readable.name : null,
    hidden: !!file && !readable,
    bytes: file ? file.size : null,
    person: null as S["LiveEvent"]["person"],
    detail: {} as Record<string, unknown>,
  };
  const noFile = {
    dataset_id: null,
    share_id: null,
    share_name: null,
    file_id: null,
    name: null,
    path: null,
    hidden: false,
    bytes: null,
  };
  switch (kind) {
    case "upload":
      return {
        ...base,
        person: person
          ? { identity_id: person.identity_id, display_name: person.display_name ?? null }
          : null,
      };
    case "recall":
      return {
        ...base,
        state: RECALL_STATES[n % RECALL_STATES.length] ?? "requested",
        person: person
          ? { identity_id: person.identity_id, display_name: person.display_name ?? null }
          : null,
        detail: { recall_id: "501", priority: "standard", via: "smb" },
      };
    case "tier":
      return {
        ...base,
        state: "moved",
        detail: { from: "hot", to: n % 2 ? "cool" : "archive", how: "in_place" },
      };
    case "heartbeat":
      return {
        ...base,
        ...noFile,
        state: edge?.guard_state ?? "armed",
        detail: { software_version: "0.3.0", outbox_depth: 4 },
      };
    case "annex":
      return {
        ...base,
        state: n % 2 ? "copied" : "verified",
        share_id: null,
        share_name: null,
        detail: { annex_id: "7", annex_name: "long-term-storage" },
      };
    default:
      return {
        ...base,
        ...noFile,
        state: n % 3 ? "verified" : "started",
        detail: { migration_id: "7", mode: "copy" },
      };
  }
}

function liveState(d: Deps): LiveState {
  const db = d.db();
  let st = lives.get(db);
  if (!st) {
    st = { seq: 0, polls: 0, events: [] };
    lives.set(db, st);
    const now = Date.now();
    for (let i = 14; i >= 0; i--) st.events.unshift(makeEvent(d, st, now - i * 55_000 - 5_000));
  }
  return st;
}

function series(seed: number): number[] {
  return Array.from({ length: 30 }, (_, i) =>
    Math.max(0, Math.round(6 + 5 * Math.sin((i + seed) / 3) + ((i * 7) % 4))),
  );
}

export function wp18Handlers(d: Deps) {
  return [
    http.get("*/v1/activity/live", async ({ request }) => {
      if (!d.isAdmin()) return d.problem(403, "role_required", "ArchiveAdmin only.");
      const url = new URL(request.url);
      const since = url.searchParams.get("since");
      const wait = Number(url.searchParams.get("wait") ?? "0");
      const st = liveState(d);
      if (since) {
        st.polls++;
        if (wait > 0) await delay(TEST ? 120 : 1600 + ((st.polls * 431) % 1400));
        if (!TEST || st.polls <= 3) {
          const fresh = 1 + (st.polls % 2);
          for (let i = 0; i < fresh; i++) st.events.unshift(makeEvent(d, st, Date.now() - i * 300));
          st.events = st.events.slice(0, 200);
        }
      }
      const cut = since ? new Date(since).getTime() - 30_000 : 0;
      const events = st.events.filter((e) => new Date(e.at).getTime() > cut);
      const edges = d.edges();
      const strip = edges.map((e, i) => ({
        edge_id: e.edge_id,
        name: e.name,
        status: e.status,
        last_heartbeat_at: e.last_heartbeat_at,
        uploads_15m: e.status === "ok" || e.status === "attention" ? 14 - i * 3 : 0,
        bytes_15m: (14 - i * 3) * 184 * 1024 ** 3,
        recalls_open: e.health.recalls_waiting ?? 0,
        outbox_depth: e.health.outbox_depth ?? null,
      }));
      const up = series(st.polls);
      return json({
        cursor: iso(Date.now()),
        events,
        counters: {
          uploads_per_min: up[29],
          uploads_15m: up.slice(-15).reduce((a, b) => a + b, 0),
          bytes_moved_15m: up.slice(-15).reduce((a, b) => a + b, 0) * 2.4 * 1024 ** 3,
          recalls_in_flight: 3 + (st.polls % 3),
          edges_online: strip.filter((e) => e.status === "ok" || e.status === "attention").length,
          edges_total: strip.filter((e) => e.status !== "pending").length,
        },
        series: {
          from: iso(Date.now() - 29 * 60_000),
          step_s: 60,
          uploads: up,
          bytes_moved: up.map((v) => Math.round(v * 2.4 * 1024 ** 3)),
        },
        edges: strip,
        // The service never says under 2 s; the tests poll faster.
        live: { waited: !!since && wait > 0, capped: false, next_poll_s: TEST ? 0 : 2 },
      });
    }),

    http.get("*/v1/edges/appliance", () => {
      if (!d.isAdmin()) return d.problem(403, "role_required", "ArchiveAdmin only.");
      return json({
        release: "0.3.0",
        images: [
          { platform: "hyperv", file: "omnifile-edge_0.3.0.vhdx", url: null },
          { platform: "vmware", file: "omnifile-edge_0.3.0.ova", url: null },
          { platform: "kvm", file: "omnifile-edge_0.3.0.qcow2", url: null },
          { platform: "azure", file: "omnifile-edge_0.3.0.vhd", url: null },
        ],
        checksums: null,
        enrolment_configured: true,
        code_ttl_s: 86_400,
      });
    }),

    http.get("*/v1/settings/prices", () => {
      if (!d.isAdmin()) return d.problem(403, "role_required", "needs the app role ArchiveAdmin");
      const s = priceState(d.db());
      return json(
        { show_to: s.show_to, set_by: s.set_by, set_at: s.set_at },
        { headers: { ETag: `"${s.version}"` } },
      );
    }),

    http.put("*/v1/settings/prices", async ({ request }) => {
      if (!d.isAdmin()) return d.problem(403, "role_required", "needs the app role ArchiveAdmin");
      const s = priceState(d.db());
      if (request.headers.get("If-Match") !== `"${s.version}"`) {
        return d.problem(412, "precondition_failed", "the setting changed; read it again");
      }
      const body = (await request.json()) as { show_to?: Role[] };
      if (!Array.isArray(body.show_to) || !body.show_to.every((r) => ROLES.includes(r))) {
        return d.problem(
          400,
          "invalid_request",
          "show_to: a list of ArchiveAdmin, DataSteward, others",
        );
      }
      s.show_to = ROLES.filter((r) => body.show_to?.includes(r));
      s.version++;
      const db = d.db();
      s.set_by = db.users[db.persona].display_name ?? null;
      s.set_at = iso(Date.now());
      if (!TEST) {
        try {
          localStorage.setItem(PRICES_KEY, JSON.stringify(s.show_to));
        } catch {
          // storage blocked: the setting lasts until the page reloads
        }
      }
      return json(
        { show_to: s.show_to, set_by: s.set_by, set_at: s.set_at },
        { headers: { ETag: `"${s.version}"` } },
      );
    }),

    http.get("*/v1/price-tables/:kind", ({ params }) => {
      if (!d.isAdmin() && !d.isSteward())
        return d.problem(403, "role_required", "ArchiveAdmin or DataSteward.");
      if (!d.isAdmin() && !pricesVisible(d.db())) {
        return d.problem(403, "forbidden", "an ArchiveAdmin has hidden prices from your role");
      }
      const t = priceTable(d.db(), String(params.kind));
      return json(t, { headers: { ETag: t.etag ?? '"0"' } });
    }),

    http.put("*/v1/price-tables/:kind", async ({ params, request }) => {
      if (!d.isAdmin()) return d.problem(403, "role_required", "needs the app role ArchiveAdmin");
      const kind = String(params.kind);
      const cur = priceTable(d.db(), kind);
      if (request.headers.get("If-Match") !== cur.etag) {
        return d.problem(412, "precondition_failed", "the table changed; read it again");
      }
      const body = (await request.json()) as S["PriceTable"];
      const st = priceState(d.db());
      const version = (st.overrides.get(kind)?.version ?? 0) + 1;
      st.overrides.set(kind, { body: { ...body, id: `${kind}/override/${body.as_of}` }, version });
      return json(priceTable(d.db(), kind));
    }),

    http.delete("*/v1/price-tables/:kind", ({ params, request }) => {
      if (!d.isAdmin()) return d.problem(403, "role_required", "needs the app role ArchiveAdmin");
      const kind = String(params.kind);
      const cur = priceTable(d.db(), kind);
      if (request.headers.get("If-Match") !== cur.etag) {
        return d.problem(412, "precondition_failed", "the table changed; read it again");
      }
      priceState(d.db()).overrides.delete(kind);
      return json(priceTable(d.db(), kind));
    }),
  ];
}
