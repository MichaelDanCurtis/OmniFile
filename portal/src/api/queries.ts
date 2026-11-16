import {
  keepPreviousData,
  useInfiniteQuery,
  useMutation,
  useQuery,
  useQueryClient,
} from "@tanstack/react-query";
import type { RestoreTarget } from "../lib/restoreNames";
import { api, idem, unwrap } from "./client";
import type {
  CostAssumptions,
  NewDataset,
  NewShare,
  PinTier,
  PolicyBody,
  ProposalState,
  RecallState,
  ShareOverride,
  ShareSettingsPatch,
} from "./types";

/** Query keys, so mutations can invalidate exactly what they change. */
export const qk = {
  shares: ["shares"] as const,
  share: (id: string) => ["shares", id] as const,
  children: (dirId: string, asOf: string | null) => ["children", dirId, asOf] as const,
  file: (id: string) => ["files", id] as const,
  versions: (fileId: string) => ["files", fileId, "versions"] as const,
  recallOptions: (fileId: string, versionId?: string) =>
    ["files", fileId, "recall-options", versionId ?? null] as const,
  recalls: (scope: string, shareId?: string, state?: string) =>
    ["recalls", scope, shareId ?? null, state ?? null] as const,
  recall: (id: string) => ["recall", id] as const,
  holds: (scope?: string, kind?: string) => ["holds", scope ?? null, kind ?? null] as const,
  pins: (shareId?: string) => ["pins", shareId ?? null] as const,
  datasets: ["datasets"] as const,
  backends: ["backends"] as const,
  activity: (days: number, datasetId: string) => ["activity", days, datasetId] as const,
  edges: () => ["edges"] as const,
  sources: ["sources"] as const,
  locations: ["locations"] as const,
  activePolicy: (datasetId: string) => ["datasets", datasetId, "policy"] as const,
  policyHistory: (datasetId: string) => ["datasets", datasetId, "policies"] as const,
  proposals: (datasetId?: string, state?: string) =>
    ["proposals", datasetId ?? null, state ?? null] as const,
  proposal: (id: string) => ["proposal", id] as const,
  groups: (q: string) => ["groups", q] as const,
  audit: (filters: Record<string, string | undefined>) => ["audit", filters] as const,
};

export function useShares() {
  return useQuery({
    queryKey: qk.shares,
    queryFn: () => unwrap(api.GET("/v1/shares", { params: { query: { limit: 500 } } })),
  });
}

export function useShare(shareId: string | undefined) {
  return useQuery({
    queryKey: qk.share(shareId ?? ""),
    enabled: !!shareId,
    queryFn: () =>
      unwrap(api.GET("/v1/shares/{share_id}", { params: { path: { share_id: shareId ?? "" } } })),
  });
}

export function useChildren(dirId: string | undefined, asOf: string | null) {
  return useInfiniteQuery({
    queryKey: qk.children(dirId ?? "", asOf),
    enabled: !!dirId,
    initialPageParam: null as string | null,
    getNextPageParam: (last: { next_cursor: string | null }) => last.next_cursor,
    placeholderData: keepPreviousData,
    queryFn: ({ pageParam }) =>
      unwrap(
        api.GET("/v1/dirs/{dir_id}/children", {
          params: {
            path: { dir_id: dirId ?? "" },
            query: {
              ...(asOf ? { as_of: asOf } : {}),
              ...(pageParam ? { cursor: pageParam } : {}),
              limit: 200,
            },
          },
        }),
      ),
  });
}

export function useFile(fileId: string | null) {
  return useQuery({
    queryKey: qk.file(fileId ?? ""),
    enabled: !!fileId,
    queryFn: () =>
      unwrap(api.GET("/v1/files/{file_id}", { params: { path: { file_id: fileId ?? "" } } })),
  });
}

export function useVersions(fileId: string | null) {
  return useQuery({
    queryKey: qk.versions(fileId ?? ""),
    enabled: !!fileId,
    queryFn: () =>
      unwrap(
        api.GET("/v1/files/{file_id}/versions", {
          params: { path: { file_id: fileId ?? "" }, query: { limit: 200 } },
        }),
      ),
  });
}

export function useRecallOptions(fileId: string | null, versionId?: string) {
  return useQuery({
    queryKey: qk.recallOptions(fileId ?? "", versionId),
    enabled: !!fileId,
    queryFn: () =>
      unwrap(
        api.GET("/v1/files/{file_id}/recall-options", {
          params: {
            path: { file_id: fileId ?? "" },
            query: versionId ? { version_id: versionId } : {},
          },
        }),
      ),
  });
}

const TERMINAL: RecallState[] = ["available", "failed", "cancelled", "superseded"];

export function useRecall(recallId: string | undefined, pollMs = 5000) {
  return useQuery({
    queryKey: qk.recall(recallId ?? ""),
    enabled: !!recallId,
    queryFn: () =>
      unwrap(
        api.GET("/v1/recalls/{recall_id}", { params: { path: { recall_id: recallId ?? "" } } }),
      ),
    refetchInterval: (query) =>
      query.state.data && TERMINAL.includes(query.state.data.state) ? false : pollMs,
  });
}

export function useRecalls(scope: "mine" | "share" | "all", shareId?: string, state?: RecallState) {
  return useQuery({
    queryKey: qk.recalls(scope, shareId, state),
    enabled: scope !== "share" || !!shareId,
    refetchInterval: 15_000,
    queryFn: () =>
      unwrap(
        api.GET("/v1/recalls", {
          params: {
            query: {
              scope,
              ...(shareId ? { share_id: shareId } : {}),
              ...(state ? { state } : {}),
              limit: 200,
            },
          },
        }),
      ),
  });
}

export function useRequestRecall() {
  const client = useQueryClient();
  return useMutation({
    mutationFn: (body: {
      file_id: string;
      version_id?: string;
      priority: string;
      due_by?: string;
    }) => unwrap(api.POST("/v1/recalls", { params: { header: idem() }, body })),
    onSuccess: (recall) => {
      client.setQueryData(qk.recall(recall.recall_id), recall);
      void client.invalidateQueries({ queryKey: ["children"] });
      void client.invalidateQueries({ queryKey: ["files", recall.file_id] });
      void client.invalidateQueries({ queryKey: ["recalls"] });
    },
  });
}

export function usePinPreview(
  request: { items: string[]; tier: PinTier; until?: string; override_archive?: boolean } | null,
) {
  return useQuery({
    queryKey: ["pin-preview", request],
    enabled: !!request && request.items.length > 0,
    placeholderData: keepPreviousData,
    queryFn: () =>
      unwrap(
        api.POST("/v1/pins/preview", {
          body: request ?? { items: [], tier: "hot" },
        }),
      ),
  });
}

function invalidateTiering(client: ReturnType<typeof useQueryClient>) {
  void client.invalidateQueries({ queryKey: ["children"] });
  void client.invalidateQueries({ queryKey: ["files"] });
  void client.invalidateQueries({ queryKey: ["pins"] });
  void client.invalidateQueries({ queryKey: ["holds"] });
}

export function useCreatePins() {
  const client = useQueryClient();
  return useMutation({
    mutationFn: (body: {
      items: string[];
      tier: PinTier;
      until?: string;
      override_archive?: boolean;
      reason: string;
    }) => unwrap(api.POST("/v1/pins", { params: { header: idem() }, body })),
    onSuccess: () => invalidateTiering(client),
  });
}

export function useReleasePin() {
  const client = useQueryClient();
  return useMutation({
    mutationFn: (pinId: string) =>
      unwrap(
        api.POST("/v1/pins/{pin_id}/release", {
          params: { path: { pin_id: pinId }, header: idem() },
        }),
      ),
    onSuccess: () => invalidateTiering(client),
  });
}

/** Holds on a share, or (with fileId) the ones that apply to one file or folder. */
export function useHolds(filter: {
  shareId?: string;
  fileId?: string;
  kind?: "keep_online" | "legal";
}) {
  const { shareId, fileId, kind } = filter;
  return useQuery({
    queryKey: qk.holds(shareId ?? fileId, kind),
    enabled: !!shareId || !!fileId,
    queryFn: () =>
      unwrap(
        api.GET("/v1/holds", {
          params: {
            query: {
              ...(shareId ? { share_id: shareId } : {}),
              ...(fileId ? { file_id: fileId } : {}),
              ...(kind ? { kind } : {}),
            },
          },
        }),
      ),
  });
}

export function useCreateHolds() {
  const client = useQueryClient();
  return useMutation({
    mutationFn: (body: {
      items: string[];
      kind: "keep_online" | "legal";
      until?: string;
      reason: string;
    }) => unwrap(api.POST("/v1/holds", { params: { header: idem() }, body })),
    onSuccess: () => invalidateTiering(client),
  });
}

export function useReleaseHold() {
  const client = useQueryClient();
  return useMutation({
    mutationFn: (holdId: string) =>
      unwrap(
        api.POST("/v1/holds/{hold_id}/release", {
          params: { path: { hold_id: holdId }, header: idem() },
        }),
      ),
    onSuccess: () => invalidateTiering(client),
  });
}

export function useRestoreVersion() {
  return useMutation({
    mutationFn: (args: { fileId: string; versionId: string; target: RestoreTarget }) =>
      unwrap(
        api.POST("/v1/files/{file_id}/versions/{version_id}/restore", {
          params: {
            path: { file_id: args.fileId, version_id: args.versionId },
            header: idem(),
          },
          body: { target: args.target },
        }),
      ),
  });
}

export function useVersionRestore(restoreId: string | null) {
  const client = useQueryClient();
  return useQuery({
    queryKey: ["version-restore", restoreId],
    enabled: !!restoreId,
    queryFn: async () => {
      const restore = await unwrap(
        api.GET("/v1/version-restores/{restore_id}", {
          params: { path: { restore_id: restoreId ?? "" } },
        }),
      );
      // A finished restore made a new file (restored, restored_timestamped) or a new
      // current version (overwrite): the folder, the files and their versions are stale.
      if (restore.state === "done") {
        void client.invalidateQueries({ queryKey: ["children"] });
        void client.invalidateQueries({ queryKey: ["files"] });
      }
      return restore;
    },
    refetchInterval: (query) => {
      const state = query.state.data?.state;
      return state === "done" || state === "failed" ? false : 2000;
    },
  });
}

export function requestDownload(fileId: string, versionId: string) {
  return unwrap(
    api.POST("/v1/files/{file_id}/versions/{version_id}/downloads", {
      params: { path: { file_id: fileId, version_id: versionId }, header: idem() },
      body: { disposition: "attachment" },
    }),
  );
}

export function getDownload(downloadId: string) {
  return unwrap(
    api.GET("/v1/downloads/{download_id}", { params: { path: { download_id: downloadId } } }),
  );
}

export function usePatchShareSettings(shareId: string) {
  const client = useQueryClient();
  return useMutation({
    mutationFn: (args: { etag: string; patch: ShareSettingsPatch }) =>
      unwrap(
        api.PATCH("/v1/shares/{share_id}/settings", {
          params: { path: { share_id: shareId }, header: { "If-Match": args.etag } },
          body: args.patch,
        }),
      ),
    onSuccess: (share) => {
      client.setQueryData(qk.share(shareId), share);
      void client.invalidateQueries({ queryKey: qk.shares });
    },
  });
}

export function useShareSettingsPreview(shareId: string, patch: ShareSettingsPatch | null) {
  return useQuery({
    queryKey: ["share-settings-preview", shareId, patch],
    enabled: !!patch && Object.keys(patch).length > 0,
    placeholderData: keepPreviousData,
    queryFn: () =>
      unwrap(
        api.POST("/v1/shares/{share_id}/settings/preview", {
          params: { path: { share_id: shareId } },
          body: patch ?? {},
        }),
      ),
  });
}

export function useActivity(days: number, datasetId: string | undefined, enabled = true) {
  return useQuery({
    queryKey: qk.activity(days, datasetId ?? ""),
    enabled,
    queryFn: () =>
      unwrap(
        api.GET("/v1/activity", {
          params: { query: { days, ...(datasetId ? { dataset_id: datasetId } : {}) } },
        }),
      ),
  });
}

/** Edges and their last heartbeat (portal-api.md 5.14), refreshed every minute. */
export function useEdges(enabled = true, refetchMs = 60_000) {
  return useQuery({
    queryKey: qk.edges(),
    enabled,
    refetchInterval: refetchMs,
    queryFn: () => unwrap(api.GET("/v1/edges")),
  });
}

/** Add an edge: a pending edge and its one-time enrolment code (portal-api.md 5.15). */
export function useAddEdge() {
  const client = useQueryClient();
  return useMutation({
    mutationFn: (name: string) =>
      unwrap(api.POST("/v1/edges", { params: { header: idem() }, body: { name } })),
    onSuccess: () => void client.invalidateQueries({ queryKey: qk.edges() }),
  });
}

/** A new code for an edge that hasn't enrolled; the previous one stops working. */
export function useNewEdgeBundle() {
  return useMutation({
    mutationFn: (edgeId: string) =>
      unwrap(
        api.POST("/v1/edges/{edge_id}/bundle", {
          params: { path: { edge_id: edgeId }, header: idem() },
        }),
      ),
  });
}

/** Revokes a lost or stolen edge (portal-api.md 5.15); its data stays. */
export function useRevokeEdge() {
  const client = useQueryClient();
  return useMutation({
    mutationFn: (edgeId: string) =>
      unwrap(
        api.POST("/v1/edges/{edge_id}/revoke", {
          params: { path: { edge_id: edgeId }, header: idem() },
          body: {},
        }),
      ),
    onSuccess: () => void client.invalidateQueries({ queryKey: qk.edges() }),
  });
}

/** Migration sources (portal-api.md 5.10): what is seeded from where. */
export function useSources(enabled = true) {
  return useQuery({
    queryKey: qk.sources,
    enabled,
    queryFn: () => unwrap(api.GET("/v1/sources", { params: { query: { limit: 500 } } })),
  });
}

/** Migration prefixes (portal-api.md 5.12): copy, verify, cut over. */
export function useLocations(enabled = true) {
  return useQuery({
    queryKey: qk.locations,
    enabled,
    queryFn: () => unwrap(api.GET("/v1/locations", { params: { query: { limit: 500 } } })),
    refetchInterval: 30_000,
  });
}

export function useDatasets(enabled = true) {
  return useQuery({
    queryKey: qk.datasets,
    enabled,
    queryFn: () => unwrap(api.GET("/v1/datasets", { params: { query: { limit: 500 } } })),
  });
}

/** The storage backends a dataset can live on (portal-api.md 5.16). */
export function useBackends(enabled = true) {
  return useQuery({
    queryKey: qk.backends,
    enabled,
    queryFn: () => unwrap(api.GET("/v1/backends")),
  });
}

/** A dataset on an edge, with its root folder and no shares yet (portal-api.md 5.16). */
export function useAddDataset() {
  const client = useQueryClient();
  return useMutation({
    mutationFn: (body: NewDataset) =>
      unwrap(api.POST("/v1/datasets", { params: { header: idem() }, body })),
    onSuccess: () => void client.invalidateQueries({ queryKey: qk.datasets }),
  });
}

/**
 * A share of a dataset's root (`path` `/`, the default) or of one of its folders
 * (`path` `/projects/2026`, once the root is shared), which its edge serves
 * (portal-api.md 5.16).
 */
export function useAddShare() {
  const client = useQueryClient();
  return useMutation({
    mutationFn: (body: NewShare) =>
      unwrap(api.POST("/v1/shares", { params: { header: idem() }, body })),
    onSuccess: (share) => {
      client.setQueryData(qk.share(share.share_id), share);
      void client.invalidateQueries({ queryKey: qk.shares });
      void client.invalidateQueries({ queryKey: qk.datasets });
    },
  });
}

/**
 * Stops serving a share (ArchiveAdmin, If-Match). Only the share goes: its dataset,
 * files and versions stay (portal-api.md 5.16).
 */
export function useRemoveShare(shareId: string) {
  const client = useQueryClient();
  return useMutation({
    mutationFn: (args: { etag: string }) =>
      unwrap(
        api.DELETE("/v1/shares/{share_id}", {
          params: { path: { share_id: shareId }, header: { "If-Match": args.etag } },
        }),
      ),
    onSuccess: () => {
      client.removeQueries({ queryKey: qk.share(shareId), exact: true });
      void client.invalidateQueries({ queryKey: qk.shares });
      void client.invalidateQueries({ queryKey: qk.datasets });
    },
  });
}

export function useActivePolicy(datasetId: string | undefined) {
  return useQuery({
    queryKey: qk.activePolicy(datasetId ?? ""),
    enabled: !!datasetId,
    retry: false,
    queryFn: () =>
      unwrap(
        api.GET("/v1/datasets/{dataset_id}/policy", {
          params: { path: { dataset_id: datasetId ?? "" } },
        }),
      ),
  });
}

export function usePolicyHistory(datasetId: string | undefined) {
  return useQuery({
    queryKey: qk.policyHistory(datasetId ?? ""),
    enabled: !!datasetId,
    queryFn: () =>
      unwrap(
        api.GET("/v1/datasets/{dataset_id}/policies", {
          params: { path: { dataset_id: datasetId ?? "" }, query: { limit: 50 } },
        }),
      ),
  });
}

export interface PreviewInput {
  dataset_id: string;
  body: PolicyBody;
  share_overrides?: ShareOverride[];
  assumptions?: CostAssumptions;
}

/** The server's preview: timeline, cost and checks. Callers debounce the input (250 ms). */
export function usePolicyPreview(input: PreviewInput | null) {
  return useQuery({
    queryKey: ["policy-preview", input],
    enabled: !!input,
    placeholderData: keepPreviousData,
    staleTime: 60_000,
    queryFn: () =>
      unwrap(
        api.POST("/v1/policies/preview", {
          body: input ?? { dataset_id: "0", body: {} },
        }),
      ),
  });
}

export function useCreatePolicy(datasetId: string) {
  const client = useQueryClient();
  return useMutation({
    mutationFn: (args: { body: PolicyBody; base_version: number | null; note?: string }) =>
      unwrap(
        api.POST("/v1/datasets/{dataset_id}/policies", {
          params: { path: { dataset_id: datasetId }, header: idem() },
          body: {
            body: args.body,
            base_version: args.base_version,
            ...(args.note ? { note: args.note } : {}),
          },
        }),
      ),
    onSuccess: (policy) => {
      client.setQueryData(qk.activePolicy(datasetId), policy);
      void client.invalidateQueries({ queryKey: ["datasets"] });
      void client.invalidateQueries({ queryKey: ["proposals"] });
    },
  });
}

export function useSubmitProposal() {
  const client = useQueryClient();
  return useMutation({
    mutationFn: (body: {
      dataset_id: string;
      body: PolicyBody;
      rationale: string;
      base_version: number | null;
      share_overrides?: ShareOverride[];
    }) => unwrap(api.POST("/v1/policies/proposals", { params: { header: idem() }, body })),
    onSuccess: () => {
      void client.invalidateQueries({ queryKey: ["proposals"] });
      void client.invalidateQueries({ queryKey: qk.datasets });
    },
  });
}

export function useProposals(datasetId?: string, state?: ProposalState) {
  return useQuery({
    queryKey: qk.proposals(datasetId, state),
    queryFn: () =>
      unwrap(
        api.GET("/v1/policies/proposals", {
          params: {
            query: {
              ...(datasetId ? { dataset_id: datasetId } : {}),
              ...(state ? { state } : {}),
              limit: 200,
            },
          },
        }),
      ),
  });
}

export function useProposal(proposalId: string | undefined) {
  return useQuery({
    queryKey: qk.proposal(proposalId ?? ""),
    enabled: !!proposalId,
    queryFn: () =>
      unwrap(
        api.GET("/v1/policies/proposals/{proposal_id}", {
          params: { path: { proposal_id: proposalId ?? "" } },
        }),
      ),
  });
}

export function useDecideProposal(proposalId: string) {
  const client = useQueryClient();
  const refresh = () => {
    void client.invalidateQueries({ queryKey: ["proposals"] });
    void client.invalidateQueries({ queryKey: qk.proposal(proposalId) });
    void client.invalidateQueries({ queryKey: ["datasets"] });
  };
  const path = { proposal_id: proposalId };
  return {
    approve: useMutation({
      mutationFn: (body: { note?: string; accept_stale?: boolean }) =>
        unwrap(
          api.POST("/v1/policies/proposals/{proposal_id}/approve", {
            params: { path, header: idem() },
            body,
          }),
        ),
      onSuccess: refresh,
    }),
    reject: useMutation({
      mutationFn: (note: string) =>
        unwrap(
          api.POST("/v1/policies/proposals/{proposal_id}/reject", {
            params: { path, header: idem() },
            body: { note },
          }),
        ),
      onSuccess: refresh,
    }),
    withdraw: useMutation({
      mutationFn: () =>
        unwrap(
          api.POST("/v1/policies/proposals/{proposal_id}/withdraw", {
            params: { path, header: idem() },
          }),
        ),
      onSuccess: refresh,
    }),
  };
}

export function useGroupSearch(q: string) {
  return useQuery({
    queryKey: qk.groups(q),
    enabled: q.trim().length > 0,
    placeholderData: keepPreviousData,
    queryFn: () =>
      unwrap(api.GET("/v1/directory/groups", { params: { query: { q: q.trim(), limit: 20 } } })),
  });
}

export function usePutShareAccess(shareId: string) {
  const client = useQueryClient();
  return useMutation({
    mutationFn: (args: {
      etag: string;
      owners_group_sid: string;
      writers_group_sid: string | null;
      readers_group_sid: string | null;
    }) =>
      unwrap(
        api.PUT("/v1/shares/{share_id}/access", {
          params: { path: { share_id: shareId }, header: { "If-Match": args.etag } },
          body: {
            owners_group_sid: args.owners_group_sid,
            writers_group_sid: args.writers_group_sid,
            readers_group_sid: args.readers_group_sid,
          },
        }),
      ),
    onSuccess: (share) => {
      client.setQueryData(qk.share(shareId), share);
      void client.invalidateQueries({ queryKey: qk.shares });
    },
  });
}

export function useWorm(shareId: string) {
  const client = useQueryClient();
  const onSuccess = (share: Awaited<ReturnType<typeof enable>>) => {
    client.setQueryData(qk.share(shareId), share);
    void client.invalidateQueries({ queryKey: qk.shares });
    void client.invalidateQueries({ queryKey: qk.datasets });
  };
  const path = { share_id: shareId };
  const enable = (args: {
    etag: string;
    retention_days: number;
    confirm_name: string;
    window_hours: number;
    quiet_s_confirmed?: boolean;
  }) =>
    unwrap(
      api.POST("/v1/shares/{share_id}/worm/enable", {
        params: { path, header: { ...idem(), "If-Match": args.etag } },
        body: {
          retention_days: args.retention_days,
          confirm_name: args.confirm_name,
          window_hours: args.window_hours,
          ...(args.quiet_s_confirmed ? { quiet_s_confirmed: true } : {}),
        },
      }),
    );
  return {
    enable: useMutation({ mutationFn: enable, onSuccess }),
    cancel: useMutation({
      mutationFn: (args: { etag: string; confirm_name: string }) =>
        unwrap(
          api.POST("/v1/shares/{share_id}/worm/cancel", {
            params: { path, header: { ...idem(), "If-Match": args.etag } },
            body: { confirm_name: args.confirm_name },
          }),
        ),
      onSuccess,
    }),
    extend: useMutation({
      mutationFn: (args: { etag: string; retention_days: number }) =>
        unwrap(
          api.POST("/v1/shares/{share_id}/worm/extend", {
            params: { path, header: { ...idem(), "If-Match": args.etag } },
            body: { retention_days: args.retention_days },
          }),
        ),
      onSuccess,
    }),
  };
}

export interface AuditFilters {
  share_id?: string;
  file_id?: string;
  actor?: string;
  action?: string;
  from?: string;
  to?: string;
}

export function useAudit(filters: AuditFilters, enabled = true) {
  return useInfiniteQuery({
    queryKey: qk.audit({ ...filters }),
    enabled,
    initialPageParam: null as string | null,
    getNextPageParam: (last: { next_cursor: string | null }) => last.next_cursor,
    queryFn: ({ pageParam }) => {
      const query = Object.fromEntries(
        Object.entries(filters).filter(([, value]) => value !== undefined && value !== ""),
      ) as AuditFilters;
      return unwrap(
        api.GET("/v1/audit", {
          params: { query: { ...query, ...(pageParam ? { cursor: pageParam } : {}), limit: 100 } },
        }),
      );
    },
  });
}
