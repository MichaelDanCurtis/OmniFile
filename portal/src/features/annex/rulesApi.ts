/**
 * Annex rule sets and dry runs (portal-api.md 5.23; annex.md 4.1-4.5, phase A2.1).
 * The rule language is the service's; every list here is open.
 */
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api, idem, unwrap } from "../../api/client";
import type { components } from "../../api/schema.gen";
import { annexKeys } from "./api";

export type RuleBody = components["schemas"]["AnnexRuleBody"];
export type Ruleset = components["schemas"]["AnnexRuleset"];
export type RulesetDetail = components["schemas"]["AnnexRulesetDetail"];
export type DryRun = components["schemas"]["AnnexDryRun"];
export type RuleCheck = components["schemas"]["AnnexRuleCheck"];

export type Trigger = "arrived" | "changed" | "age" | "deleted_at_source";

/** One rule as the builder edits it: the body's shape, typed. */
export interface Rule {
  id: string;
  label?: string;
  trigger: Trigger;
  conditions: Conditions;
  actions: Action[];
}

export interface Conditions {
  path?: string[];
  extension?: string[];
  size_gte?: number;
  size_lt?: number;
  age_since_arrival_days_gt?: number;
  modified_age_days_gt?: number;
  accessed_age_days_gt?: number;
  owner_sids?: string[];
  group_sids?: string[];
  content_type?: string[];
}

export type Action =
  | { exclude: true }
  | { copy_to: { kind: "tier"; dataset_id?: string } }
  | { remove_from_source: true }
  | { remove_after_days: number }
  | { policy: { policy_id: string } }
  | { tag: { key: string; value: string } }
  | { keep_copy: true };

export const TRIGGER_LABEL: Record<string, string> = {
  arrived: "When a file lands",
  changed: "When a copied file changes",
  age: "As files get older (checked daily)",
  deleted_at_source: "When a file is deleted on the NAS",
};

export const CHECK_LABEL: Record<string, string> = {
  missing_default: "Needs a last rule for everything else",
  unreachable: "Never matches",
  remove_without_copy: "Removes without a copy",
  remove_without_age: "Removes without an age",
  remove_before_quiet: "Removes before the quiet period ends",
  return_into_drop_folder: "Would go straight out again",
  return_without_hold: "Needs a hold",
  overwrite_outside_annex: "Replaces only inside this annex",
  unknown_location: "No such place",
  location_unreachable: "Can't be reached",
};

export function rulesOf(body: RuleBody | undefined | null): Rule[] {
  return (body?.rules ?? []) as unknown as Rule[];
}

export function removes(r: Rule): boolean {
  return r.actions.some((a) => "remove_from_source" in a || "remove_after_days" in a);
}

export function copies(r: Rule): boolean {
  return r.actions.some((a) => "copy_to" in a);
}

export function excludes(r: Rule): boolean {
  return r.actions.some((a) => "exclude" in a);
}

export function useRulesets(annexId: string) {
  return useQuery({
    queryKey: ["annexes", annexId, "rulesets"],
    queryFn: () =>
      unwrap(
        api.GET("/v1/annexes/{annex_id}/rulesets", { params: { path: { annex_id: annexId } } }),
      ),
  });
}

export function useRuleset(annexId: string, version: number | null) {
  return useQuery({
    queryKey: ["annexes", annexId, "rulesets", version],
    enabled: version !== null,
    queryFn: () =>
      unwrap(
        api.GET("/v1/annexes/{annex_id}/rulesets/{version}", {
          params: { path: { annex_id: annexId, version: version ?? 0 } },
        }),
      ),
  });
}

export function useProposeRuleset(annexId: string) {
  const client = useQueryClient();
  return useMutation({
    mutationFn: (b: { body: RuleBody; rationale?: string }) =>
      unwrap(
        api.POST("/v1/annexes/{annex_id}/rulesets", {
          params: { path: { annex_id: annexId }, header: idem() },
          body: b,
        }),
      ),
    onSuccess: () => void client.invalidateQueries({ queryKey: annexKeys.annex(annexId) }),
  });
}

export function useDecideRuleset(annexId: string) {
  const client = useQueryClient();
  return useMutation({
    mutationFn: ({
      version,
      approve,
      etag,
      note,
    }: {
      version: number;
      approve: boolean;
      etag: string;
      note?: string;
    }) => {
      const opts = {
        params: {
          path: { annex_id: annexId, version },
          header: { "If-Match": etag, ...idem() },
        },
        body: note ? { note } : {},
      };
      return unwrap(
        approve
          ? api.POST("/v1/annexes/{annex_id}/rulesets/{version}/approve", opts)
          : api.POST("/v1/annexes/{annex_id}/rulesets/{version}/reject", opts),
      );
    },
    onSuccess: () => void client.invalidateQueries({ queryKey: annexKeys.annexes }),
  });
}

export function useCreateDryRun(annexId: string) {
  return useMutation({
    mutationFn: (body: RuleBody | null) =>
      unwrap(
        api.POST("/v1/annexes/{annex_id}/dry-runs", {
          params: { path: { annex_id: annexId }, header: idem() },
          body: { body },
        }),
      ),
  });
}

/** A dry run, polled until the edge has answered. */
export function useDryRun(annexId: string, runId: string | null) {
  return useQuery({
    queryKey: ["annexes", annexId, "dry-runs", runId],
    enabled: runId !== null,
    refetchInterval: (q) =>
      q.state.data?.state === "requested" || q.state.data?.state === "running" ? 1000 : false,
    queryFn: () =>
      unwrap(
        api.GET("/v1/annexes/{annex_id}/dry-runs/{run_id}", {
          params: { path: { annex_id: annexId, run_id: runId ?? "" } },
        }),
      ),
  });
}

/** A count in a report. Below 5, those who can't see paths get `fewer_than`
 * instead of the numbers (a small count can name a file). */
export interface Count {
  files: number | null;
  bytes: number | null;
  fewer_than?: number;
}

/** The parts of a dry-run report the portal reads. */
export interface ReportRule extends Count {
  id: string;
  trigger: string;
  samples?: string[];
  copies?: boolean;
  removes?: boolean;
}

export interface Report {
  files?: number;
  bytes?: number;
  rules?: ReportRule[];
  copied?: Count;
  excluded?: Count;
  undecided?: Count;
  removals?: Count & {
    now?: Count;
    undecided?: Count;
    by_day?: ({ date: string } & Count)[];
  };
}

/** "12 files (3 GB)", or "fewer than 5 files". */
export function countText(c: Count | undefined, formatBytes: (n: number) => string): string {
  if (!c) return "0 files";
  if (c.files === null) return `fewer than ${c.fewer_than ?? 5} files`;
  return `${c.files} files (${formatBytes(c.bytes ?? 0)})`;
}

/** What a proposal changes, rule by rule, against the running set. Order
 * matters (the first matching rule of a trigger wins), so a rule whose place
 * among its trigger's rules changed is a change too. */
export function ruleDiff(
  next: Rule[],
  running: Rule[],
): { added: Rule[]; removed: Rule[]; changed: Rule[]; moved: Rule[] } {
  const key = (r: Rule) =>
    JSON.stringify({ t: r.trigger, c: r.conditions, a: r.actions, l: r.label ?? null });
  const places = (rs: Rule[]) => {
    const seen = new Map<string, number>();
    const out = new Map<string, { rule: Rule; place: number }>();
    for (const r of rs) {
      const n = seen.get(r.trigger) ?? 0;
      seen.set(r.trigger, n + 1);
      out.set(r.id, { rule: r, place: n });
    }
    return out;
  };
  const was = places(running);
  const now = places(next);
  return {
    added: next.filter((r) => !was.has(r.id)),
    removed: running.filter((r) => !now.has(r.id)),
    changed: next.filter((r) => {
      const old = was.get(r.id);
      return old !== undefined && key(old.rule) !== key(r);
    }),
    moved: next.filter((r) => {
      const old = was.get(r.id);
      return old !== undefined && old.place !== now.get(r.id)?.place;
    }),
  };
}

export function reportOf(run: DryRun | undefined): Report | null {
  const r: Report | null | undefined = run?.state === "done" ? run.report : null;
  return r ?? null;
}

export type AnnexRemoval = components["schemas"]["AnnexRemoval"];

/** The annex's removals (phase A2.2), newest first, filtered by the files' ACLs. */
export function useRemovals(
  annexId: string,
  state: "pending" | "done" | "cancelled" | "stuck" | "",
) {
  return useQuery({
    queryKey: ["annexes", annexId, "removals", state],
    refetchInterval: 10_000,
    queryFn: () =>
      unwrap(
        api.GET("/v1/annexes/{annex_id}/removals", {
          params: {
            path: { annex_id: annexId },
            query: { limit: 100, ...(state ? { state } : {}) },
          },
        }),
      ),
  });
}

/** Decision 59: an ArchiveAdmin ends the dry run, signed in just now. */
export function useConfirmRemovals(annexId: string) {
  const client = useQueryClient();
  return useMutation({
    mutationFn: (etag: string) =>
      unwrap(
        api.POST("/v1/annexes/{annex_id}/removals/confirm", {
          params: { path: { annex_id: annexId }, header: { "If-Match": etag, ...idem() } },
          body: {},
        }),
      ),
    onSuccess: () => void client.invalidateQueries({ queryKey: annexKeys.annexes }),
  });
}
