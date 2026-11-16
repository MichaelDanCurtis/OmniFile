import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api, idem, unwrap } from "../../../api/client";
import type { components, paths } from "../../../api/schema.gen";
import { polling } from "../api";

type S = components["schemas"];
export type ConsoleRow = S["ConsoleRow"];
export type AttentionItem = S["AttentionItem"];
export type MigrationWork = S["MigrationWork"];
export type MigrationAgents = S["MigrationAgents"];
export type MoverAgent = S["MoverAgent"];
export type MigrationPlan = S["MigrationPlan"];
export type MigrationWave = S["MigrationWave"];
export type MigrationPlanCreate = S["MigrationPlanCreate"];
export type WavePatch = S["WavePatch"];
export type MigrationSplit = S["MigrationSplit"];
type SplitQuery = NonNullable<paths["/v1/migration-plans/split"]["get"]["parameters"]["query"]>;

const keys = {
  console: ["migration-console"] as const,
  work: (id: string) => ["migrations", id, "work"] as const,
  agents: ["migration-agents"] as const,
  plans: ["migration-plans"] as const,
  plan: (id: string) => ["migration-plans", id] as const,
  split: (q: SplitQuery) => ["migration-split", q] as const,
};

/** The console, refreshed every 15 seconds (it is computed from the catalog). */
export function useConsole(enabled = true) {
  return useQuery({
    queryKey: keys.console,
    enabled,
    refetchInterval: Math.max(polling.ms, 15_000),
    queryFn: () => unwrap(api.GET("/v1/migration-console")),
  });
}

export function useWork(migrationId: string | null) {
  return useQuery({
    queryKey: keys.work(migrationId ?? ""),
    enabled: !!migrationId,
    refetchInterval: Math.max(polling.ms, 15_000),
    queryFn: () =>
      unwrap(
        api.GET("/v1/migrations/{migration_id}/agents", {
          params: { path: { migration_id: migrationId ?? "" } },
        }),
      ),
  });
}

export function useAgents(enabled = true) {
  return useQuery({
    queryKey: keys.agents,
    enabled,
    refetchInterval: 60_000,
    queryFn: () => unwrap(api.GET("/v1/migration-agents")),
  });
}

export function usePlans(enabled = true) {
  return useQuery({
    queryKey: keys.plans,
    enabled,
    queryFn: () => unwrap(api.GET("/v1/migration-plans", { params: { query: { limit: 500 } } })),
  });
}

export function usePlan(planId: string | undefined) {
  return useQuery({
    queryKey: keys.plan(planId ?? ""),
    enabled: !!planId,
    refetchInterval: Math.max(polling.ms, 30_000),
    queryFn: () =>
      unwrap(
        api.GET("/v1/migration-plans/{plan_id}", {
          params: { path: { plan_id: planId ?? "" } },
        }),
      ),
  });
}

export function useSplit(q: SplitQuery | null) {
  return useQuery({
    queryKey: keys.split(q ?? { source_id: "" }),
    enabled: !!q?.source_id,
    queryFn: () =>
      unwrap(api.GET("/v1/migration-plans/split", { params: { query: q ?? { source_id: "" } } })),
  });
}

function usePlanWrite<A>(fn: (args: A) => Promise<MigrationPlan>) {
  const client = useQueryClient();
  return useMutation({
    mutationFn: fn,
    onSuccess: (plan) => {
      client.setQueryData(keys.plan(plan.plan_id), plan);
      void client.invalidateQueries({ queryKey: keys.plans });
      void client.invalidateQueries({ queryKey: keys.console });
    },
  });
}

export function useCreatePlan() {
  return usePlanWrite((body: MigrationPlanCreate) =>
    unwrap(api.POST("/v1/migration-plans", { params: { header: idem() }, body })),
  );
}

export function useDecidePlan(planId: string) {
  return usePlanWrite((args: { decision: "approve" | "reject" | "cancel"; note?: string }) => {
    const params = { path: { plan_id: planId }, header: idem() };
    const body = args.note ? { note: args.note } : {};
    return unwrap(
      args.decision === "approve"
        ? api.POST("/v1/migration-plans/{plan_id}/approve", { params, body })
        : args.decision === "reject"
          ? api.POST("/v1/migration-plans/{plan_id}/reject", { params, body })
          : api.POST("/v1/migration-plans/{plan_id}/cancel", { params, body: {} }),
    );
  });
}

export function useReschedule(planId: string) {
  return usePlanWrite((args: { seq: number; patch: WavePatch }) =>
    unwrap(
      api.PATCH("/v1/migration-plans/{plan_id}/waves/{seq}", {
        params: { path: { plan_id: planId, seq: args.seq } },
        body: args.patch,
      }),
    ),
  );
}

export function useWaveCutover(planId: string) {
  return usePlanWrite((seq: number) =>
    unwrap(
      api.POST("/v1/migration-plans/{plan_id}/waves/{seq}/cutover", {
        params: { path: { plan_id: planId, seq }, header: idem() },
        body: {},
      }),
    ),
  );
}

/** Retry a wave's failed migrations, or cancel the wave's migrations (the plan's own routes). */
export function useWaveAction(planId: string) {
  return usePlanWrite((args: { seq: number; action: "retry" | "cancel" }) => {
    const params = { path: { plan_id: planId, seq: args.seq }, header: idem() };
    return unwrap(
      args.action === "retry"
        ? api.POST("/v1/migration-plans/{plan_id}/waves/{seq}/retry", { params, body: {} })
        : api.POST("/v1/migration-plans/{plan_id}/waves/{seq}/cancel", { params, body: {} }),
    );
  });
}

/** Copy again, Restore again, Retry: the console's in-place fixes. */
export function useFix() {
  const client = useQueryClient();
  return useMutation({
    mutationFn: (args: { migrationId: string; fix: "rerun" | "restore_again" | "retry" }) => {
      const params = { path: { migration_id: args.migrationId }, header: idem() };
      return unwrap(
        args.fix === "rerun"
          ? api.POST("/v1/migrations/{migration_id}/rerun", { params, body: {} })
          : args.fix === "restore_again"
            ? api.POST("/v1/migrations/{migration_id}/restore-again", { params, body: {} })
            : api.POST("/v1/migrations/{migration_id}/retry", { params, body: {} }),
      );
    },
    onSuccess: () => {
      void client.invalidateQueries({ queryKey: keys.console });
      void client.invalidateQueries({ queryKey: ["migrations"] });
    },
  });
}
