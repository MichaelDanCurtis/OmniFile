import { useInfiniteQuery, useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api, idem, unwrap } from "../../api/client";
import { qk } from "../../api/queries";
import type { components, paths } from "../../api/schema.gen";

type S = components["schemas"];

export type Migration = S["Migration"];
export type Source = S["Source"];
export type SourceBackend = S["SourceBackend"];
export type SourceCreate = S["SourceCreate"];
export type SourceInventory = S["SourceInventory"];
export type MigrationCreate = S["MigrationCreate"];
export type TombstoneReport = S["TombstoneReport"];
export type Tombstone = S["Tombstone"];
export type StuckKind = S["StuckCopies"]["kind"];
export type MigrationExclude =
  paths["/v1/migrations/{migration_id}/exclude"]["post"]["requestBody"]["content"]["application/json"];

/**
 * How often the migration pages poll while something is moving. Tests turn
 * it down so a seeding source settles in milliseconds.
 */
export const polling = { ms: 3000 };

const keys = {
  migrations: ["migrations"] as const,
  migration: (id: string) => ["migrations", id] as const,
  tombstones: (id: string) => ["migrations", id, "tombstones"] as const,
  stuckCopies: (id: string, kind: StuckKind) => ["migrations", id, "stuck-copies", kind] as const,
  source: (id: string) => ["sources", id] as const,
  sourceBackends: ["source-backends"] as const,
};

const SETTLED_SOURCE = new Set(["seeded", "failed"]);
const SETTLED_MIGRATION = new Set(["cut_over", "cancelled", "failed"]);

/** Every migration (portal-api.md 5.17), newest first. */
export function useMigrations(enabled = true) {
  return useQuery({
    queryKey: keys.migrations,
    enabled,
    refetchInterval: 30_000,
    queryFn: () => unwrap(api.GET("/v1/migrations", { params: { query: { limit: 500 } } })),
  });
}

/** One migration, polled until it settles (cut over, cancelled or failed). */
export function useMigration(migrationId: string | undefined) {
  return useQuery({
    queryKey: keys.migration(migrationId ?? ""),
    enabled: !!migrationId,
    refetchInterval: (query) =>
      query.state.data && SETTLED_MIGRATION.has(query.state.data.state) ? false : polling.ms,
    queryFn: () =>
      unwrap(
        api.GET("/v1/migrations/{migration_id}", {
          params: { path: { migration_id: migrationId ?? "" } },
        }),
      ),
  });
}

/** One source, polled while it is being catalogued. */
export function useSource(sourceId: string | null | undefined) {
  return useQuery({
    queryKey: keys.source(sourceId ?? ""),
    enabled: !!sourceId,
    refetchInterval: (query) =>
      query.state.data && SETTLED_SOURCE.has(query.state.data.state) ? false : polling.ms,
    queryFn: () =>
      unwrap(
        api.GET("/v1/sources/{source_id}", { params: { path: { source_id: sourceId ?? "" } } }),
      ),
  });
}

/** The S3 backends an admin configured, with their buckets (portal-api.md 5.10). */
export function useSourceBackends(enabled = true) {
  return useQuery({
    queryKey: keys.sourceBackends,
    enabled,
    queryFn: () => unwrap(api.GET("/v1/source-backends")),
  });
}

/** The cutover report rows the signed-in admin can read (portal-api.md 5.18). */
export function useTombstones(migrationId: string | undefined, enabled = true) {
  return useQuery({
    queryKey: keys.tombstones(migrationId ?? ""),
    enabled: enabled && !!migrationId,
    queryFn: () =>
      unwrap(
        api.GET("/v1/migrations/{migration_id}/tombstones", {
          params: { path: { migration_id: migrationId ?? "" }, query: { limit: 100 } },
        }),
      ),
  });
}

/** Which objects the worker last counted as stuck, for the Exclude dialog. */
export function useStuckCopies(migrationId: string, kind: StuckKind, enabled: boolean) {
  return useInfiniteQuery({
    queryKey: keys.stuckCopies(migrationId, kind),
    enabled,
    initialPageParam: null as string | null,
    getNextPageParam: (last: { next_cursor: string | null }) => last.next_cursor,
    queryFn: ({ pageParam }) =>
      unwrap(
        api.GET("/v1/migrations/{migration_id}/stuck-copies", {
          params: {
            path: { migration_id: migrationId },
            query: { kind, ...(pageParam ? { cursor: pageParam } : {}), limit: 200 },
          },
        }),
      ),
  });
}

export function useRegisterSource() {
  const client = useQueryClient();
  return useMutation({
    mutationFn: (body: SourceCreate) =>
      unwrap(api.POST("/v1/sources", { params: { header: idem() }, body })),
    onSuccess: (source) => {
      client.setQueryData(keys.source(source.source_id), source);
      void client.invalidateQueries({ queryKey: qk.sources });
      void client.invalidateQueries({ queryKey: qk.datasets });
    },
  });
}

export function useRetrySource() {
  const client = useQueryClient();
  return useMutation({
    mutationFn: (sourceId: string) =>
      unwrap(
        api.POST("/v1/sources/{source_id}/retry", {
          params: { path: { source_id: sourceId }, header: idem() },
          body: {},
        }),
      ),
    onSuccess: (source) => {
      client.setQueryData(keys.source(source.source_id), source);
      void client.invalidateQueries({ queryKey: qk.sources });
    },
  });
}

export function useStartMigration() {
  const client = useQueryClient();
  return useMutation({
    mutationFn: (body: MigrationCreate) =>
      unwrap(api.POST("/v1/migrations", { params: { header: idem() }, body })),
    onSuccess: (m) => {
      client.setQueryData(keys.migration(m.migration_id), m);
      void client.invalidateQueries({ queryKey: keys.migrations });
      void client.invalidateQueries({ queryKey: qk.locations });
    },
  });
}

type Action = "cutover" | "cancel" | "retry";

/** Cut over (manual), cancel, or retry (failed): each takes `{}` (portal-api.md 5.17). */
export function useMigrationAction(migrationId: string) {
  const client = useQueryClient();
  return useMutation({
    mutationFn: (action: Action) => {
      const params = { path: { migration_id: migrationId }, header: idem() };
      const call =
        action === "cutover"
          ? api.POST("/v1/migrations/{migration_id}/cutover", { params, body: {} })
          : action === "cancel"
            ? api.POST("/v1/migrations/{migration_id}/cancel", { params, body: {} })
            : api.POST("/v1/migrations/{migration_id}/retry", { params, body: {} });
      return unwrap(call);
    },
    onSuccess: (m) => {
      client.setQueryData(keys.migration(m.migration_id), m);
      void client.invalidateQueries({ queryKey: keys.migrations });
    },
  });
}

export type SourceScan = S["SourceScan"];
export type SourceScanResult = S["SourceScanResult"];
export type SourceScanCreate =
  paths["/v1/source-scans"]["post"]["requestBody"]["content"]["application/json"];

/** How often a running scan is read. Tests turn it down. */
export const scanPolling = { ms: 1000 };
const SETTLED_SCAN = new Set(["done", "capped", "failed"]);
export const scanSettled = (state: string) => SETTLED_SCAN.has(state);

/** Scan a source before registering it (cloud-connect.md 4): a capped, read-only listing. */
export function useStartScan() {
  const client = useQueryClient();
  return useMutation({
    mutationFn: (body: SourceScanCreate) =>
      unwrap(api.POST("/v1/source-scans", { params: { header: idem() }, body })),
    onSuccess: (scan) => client.setQueryData(["source-scans", scan.scan_id], scan),
  });
}

/** One scan, polled every second until it is done, capped or failed. */
export function useSourceScan(scanId: string | null) {
  return useQuery({
    queryKey: ["source-scans", scanId ?? ""],
    enabled: !!scanId,
    refetchInterval: (query) =>
      query.state.data && scanSettled(query.state.data.state) ? false : scanPolling.ms,
    queryFn: () =>
      unwrap(
        api.GET("/v1/source-scans/{scan_id}", { params: { path: { scan_id: scanId ?? "" } } }),
      ),
  });
}

/** Excludes objects that can never be verified (ArchiveAdmin, re-auth). */
export function useExclude(migrationId: string) {
  const client = useQueryClient();
  return useMutation({
    mutationFn: (body: MigrationExclude) =>
      unwrap(
        api.POST("/v1/migrations/{migration_id}/exclude", {
          params: { path: { migration_id: migrationId }, header: idem() },
          body,
        }),
      ),
    onSuccess: (m) => {
      client.setQueryData(keys.migration(m.migration_id), m);
      void client.invalidateQueries({ queryKey: keys.migrations });
    },
  });
}
