import { http, HttpResponse, type JsonBodyType } from "msw";
import type { Me, PersonRef, ProblemCode, RecallState, Share } from "../api/types";
import type { components } from "../api/schema.gen";
import type { MockDb, MockFile, MockRecall } from "./db";

/**
 * Mock of the visual views' routes (portal-api.md 5.22): the file journey,
 * the share map and its node lists. The tiers mirror the service's backend
 * capabilities (service/src/omnifile_service/backends/sim/capabilities.py).
 */

type Journey = components["schemas"]["FileJourney"];
type ShareMap = components["schemas"]["ShareMap"];
type MapFile = components["schemas"]["MapFile"];
type MapCount = components["schemas"]["MapCount"];

export interface VisualContext {
  db: () => MockDb;
  me: () => Me & { identity_id: string };
  isAdmin: () => boolean;
  isSteward: () => boolean;
  myAccess: (share: Share) => Share["my_access"];
  readableFile: (id: string) => MockFile | null;
  ancestors: (file: MockFile) => MockFile[];
  recallState: (r: MockRecall) => RecallState;
  problem: (status: number, code: ProblemCode, detail: string) => Response;
}

const LADDER: Record<string, Journey["tiers"]> = {
  azure_blob: [
    {
      logical: "hot",
      classes: [{ native_class: "Hot", minimum_days: null, needs_restore: false }],
    },
    {
      logical: "warm",
      classes: [{ native_class: "Cool", minimum_days: 30, needs_restore: false }],
    },
    {
      logical: "cold",
      classes: [{ native_class: "Cold", minimum_days: 90, needs_restore: false }],
    },
    {
      logical: "archive",
      classes: [{ native_class: "Archive", minimum_days: 180, needs_restore: true }],
    },
    {
      logical: "smart",
      classes: [{ native_class: "Smart", minimum_days: null, needs_restore: false }],
    },
  ],
  s3: [
    {
      logical: "hot",
      classes: [{ native_class: "STANDARD", minimum_days: null, needs_restore: false }],
    },
    {
      logical: "warm",
      classes: [{ native_class: "STANDARD_IA", minimum_days: 30, needs_restore: false }],
    },
    {
      logical: "cold",
      classes: [{ native_class: "GLACIER_IR", minimum_days: 90, needs_restore: false }],
    },
    {
      logical: "archive",
      classes: [
        { native_class: "GLACIER", minimum_days: 90, needs_restore: true },
        { native_class: "DEEP_ARCHIVE", minimum_days: 180, needs_restore: true },
      ],
    },
    {
      logical: "smart",
      classes: [{ native_class: "INTELLIGENT_TIERING", minimum_days: null, needs_restore: false }],
    },
  ],
};

const S3_CLASSES = new Set(["STANDARD", "STANDARD_IA", "GLACIER_IR", "GLACIER", "DEEP_ARCHIVE"]);
const ACTIVE: RecallState[] = ["requested", "rehydrating", "copying", "downloading"];
const EDGE: Record<string, string[]> = {
  resident: ["resident"],
  stub: ["stub", "recalling"],
  arriving: ["landing", "uploading"],
  remote: ["remote"],
};
const json = (body: unknown) => HttpResponse.json(body as JsonBodyType);

function account(kind: string, layout: string | undefined, tier: string | null) {
  if (kind === "s3" || !tier) return null;
  if (layout === "two_account") return tier === "archive" ? "archive" : "live";
  if (layout === "single_account") return "single";
  return null;
}

export function visualHandlers(ctx: VisualContext) {
  const dataset = (share: Share) =>
    ctx.db().datasets.find((d) => d.dataset_id === share.dataset_id);
  const shareOf = (id: string) => ctx.db().shares.find((s) => s.share_id === id);
  const kindOf = (f: MockFile, home: string) =>
    f.tier.native_class && S3_CLASSES.has(f.tier.native_class) ? "s3" : home;
  const filesOf = (share: Share) =>
    [...ctx.db().files.values()].filter(
      (f) => f.share_id === share.share_id && f.kind === "file" && !f.deleted_at,
    );
  const seeShare = (share: Share | undefined): share is Share =>
    !!share && (ctx.isAdmin() || ctx.isSteward() || ctx.myAccess(share).can_read_root);
  const activeRecall = (f: MockFile) =>
    ctx
      .db()
      .recalls.find((r) => r.recall.file_id === f.file_id && ACTIVE.includes(ctx.recallState(r)));

  return [
    http.get("*/v1/files/:id/journey", ({ params }) => {
      const file = ctx.readableFile(String(params.id));
      if (!file) return ctx.problem(404, "not_found", "No such file.");
      const share = shareOf(file.share_id) as Share;
      const ds = dataset(share);
      const home = ds?.backend_kind ?? "azure_blob";
      const kind = kindOf(file, home);
      const me = ctx.me();
      const privileged = ctx.isAdmin() || ctx.isSteward() || me.owner_of.includes(share.share_id);
      const who = (by: PersonRef | undefined) =>
        by && (privileged || by.identity_id === me.identity_id) ? by : null;
      const chain = [file, ...ctx.ancestors(file).reverse()].map((f) => f.file_id);
      const now = Date.now();
      const live = (until: string | null | undefined) => !until || new Date(until).getTime() > now;
      const pin = chain
        .map((id) => ctx.db().pins.find((p) => p.file_id === id && !p.released_at && live(p.until)))
        .find(Boolean);
      const holds = ctx
        .db()
        .holds.filter((h) => chain.includes(h.file_id) && !h.released_at && live(h.until));
      const body: Journey = {
        file_id: file.file_id,
        backend: {
          kind,
          role: kind === home ? "home" : "source",
          storage_layout: ds?.storage_layout ?? "two_account",
          account: account(kind, ds?.storage_layout, file.tier.logical ?? null),
          migration_state: kind === home ? (ds?.migration_state ?? "steady") : "migrating",
        },
        tiers: LADDER[kind] ?? [],
        pin: pin
          ? {
              pin_id: pin.pin_id,
              tier: pin.tier,
              until: pin.until ?? null,
              override_archive: pin.override_archive ?? false,
              set_at: pin.set_at,
              set_by: who(pin.set_by),
              reason: privileged ? (pin.reason ?? null) : null,
              inherited_from: pin.file_id === file.file_id ? null : pin.file_id,
            }
          : null,
        holds: holds.map((h) => ({
          hold_id: h.hold_id,
          kind: h.kind,
          until: h.until ?? null,
          set_at: h.set_at,
          set_by: who(h.set_by),
          reason: privileged ? h.reason : null,
          inherited_from: h.file_id === file.file_id ? null : h.file_id,
        })),
      };
      return json(body);
    }),

    http.get("*/v1/shares/:id/map", ({ params }) => {
      const share = shareOf(String(params.id));
      if (!seeShare(share)) return ctx.problem(404, "not_found", "No such share.");
      const ds = dataset(share);
      const home = ds?.backend_kind ?? "azure_blob";
      const admin = ctx.isAdmin();
      // Whole-share counts for ArchiveAdmin and DataSteward (owner, 2026-11-04).
      const whole = admin || ctx.isSteward();
      // The mock's ACLs are per share: anyone else who can't read the root counts nothing.
      const counted = whole || ctx.myAccess(share).can_read_root ? filesOf(share) : [];
      const edge: ShareMap["edge"] = {
        resident: { files: 0, bytes: 0, local_bytes: 0 },
        stub: { files: 0, bytes: 0, local_bytes: 0 },
        arriving: { files: 0, bytes: 0, local_bytes: 0 },
        remote: { files: 0, bytes: 0, local_bytes: 0 },
      };
      const stores = new Map<string, ShareMap["stores"][number]>();
      const steps = { requested: 0, rehydrating: 0, downloading: 0 };
      for (const f of counted) {
        const bucket = Object.keys(EDGE).find((b) => EDGE[b]?.includes(f.state)) ?? f.state;
        // A state this mock doesn't know (another backend's) is in no bucket.
        const e = (edge as Record<string, MapCount | undefined>)[bucket];
        if (e) {
          e.files += 1;
          e.bytes += f.size;
          e.local_bytes += f.state === "resident" ? f.size : Math.min(f.size, 4_000_000);
        }
        if (f.tier.logical || f.tier.native_class) {
          const kind = kindOf(f, home);
          const store = stores.get(kind) ?? {
            backend_kind: kind,
            role: kind === home ? "home" : "source",
            tiers: [],
          };
          stores.set(kind, store);
          const row = store.tiers.find(
            (t) => t.logical_tier === f.tier.logical && t.native_class === f.tier.native_class,
          );
          if (row) {
            row.files += 1;
            row.bytes += f.size;
          } else {
            store.tiers.push({
              logical_tier: f.tier.logical ?? null,
              native_class: f.tier.native_class ?? null,
              account: account(kind, ds?.storage_layout, f.tier.logical ?? null),
              files: 1,
              bytes: f.size,
            });
          }
        }
        const r = activeRecall(f);
        if (r) {
          const s = ctx.recallState(r);
          steps[s === "copying" ? "rehydrating" : (s as keyof typeof steps)] += 1;
        }
      }
      const order = ["hot", "warm", "cold", "archive", "smart"];
      const rank = (t: string | null | undefined) => {
        const i = order.indexOf(t ?? "");
        return i < 0 ? 99 : i;
      };
      for (const s of stores.values())
        s.tiers.sort((a, b) => rank(a.logical_tier) - rank(b.logical_tier));
      const body: ShareMap = {
        share_id: share.share_id,
        dataset_id: share.dataset_id,
        scope: whole ? "all" : "readable",
        basis: "rollup",
        // The worker's roll-up; the mock says its counts are true as of 2 minutes ago.
        computed_at: new Date(Date.now() - 2 * 60_000).toISOString(),
        home: { backend_kind: home, storage_layout: ds?.storage_layout ?? "two_account" },
        source: stores.has("s3") && home !== "s3" ? { backend_kind: "s3" } : null,
        migration_state: ds?.migration_state ?? "steady",
        edge,
        stores: [...stores.values()].sort(
          (a, b) => Number(b.role === "home") - Number(a.role === "home"),
        ),
        recalls: { open: steps.requested + steps.rehydrating + steps.downloading, by_step: steps },
        migrations: admin ? [] : null,
      };
      return json(body);
    }),

    http.get("*/v1/shares/:id/map/files", ({ params, request }) => {
      const share = shareOf(String(params.id));
      if (!seeShare(share)) return ctx.problem(404, "not_found", "No such share.");
      const url = new URL(request.url);
      const edge = url.searchParams.get("edge");
      const tier = url.searchParams.get("tier");
      const role = url.searchParams.get("role");
      const recalling = url.searchParams.get("recalling");
      if (!edge && !tier && !recalling) {
        return ctx.problem(400, "invalid_request", "Name a node: edge, tier or recalling.");
      }
      const home = dataset(share)?.backend_kind ?? "azure_blob";
      // Names files: readable ones only, whatever the role.
      const readable = ctx.myAccess(share).can_read_root ? filesOf(share) : [];
      const items: MapFile[] = readable
        .filter((f) => !edge || (EDGE[edge] ?? []).includes(f.state))
        .filter((f) => !tier || f.tier.logical === tier)
        .filter((f) => !role || (kindOf(f, home) === home ? "home" : "source") === role)
        .filter((f) => !recalling || !!activeRecall(f))
        .sort((a, b) => Number(a.file_id) - Number(b.file_id))
        .map((f) => ({
          file_id: f.file_id,
          name: f.name,
          parent_id: f.parent_id,
          size: f.size,
          state: f.state,
          resident_bytes: f.state === "resident" ? f.size : Math.min(f.size, 4_000_000),
          tier: f.tier,
        }));
      const limit = Math.min(Number(url.searchParams.get("limit") ?? 50), 200);
      const start = Number(url.searchParams.get("cursor") ?? 0);
      return json({
        items: items.slice(start, start + limit),
        next_cursor: start + limit < items.length ? String(start + limit) : null,
      });
    }),
  ];
}
