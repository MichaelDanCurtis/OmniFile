import { useInfiniteQuery, useQuery } from "@tanstack/react-query";
import { api, unwrap } from "../../../api/client";
import type { components } from "../../../api/schema.gen";

/** The visual views' routes (portal-api.md 5.22). Read-only. */
export type FileJourney = components["schemas"]["FileJourney"];
export type ShareMap = components["schemas"]["ShareMap"];
export type MapFile = components["schemas"]["MapFile"];
export type MapCount = components["schemas"]["MapCount"];

/** A node of the share map, as the `/map/files` filter takes it. */
export type MapNode =
  | { edge: "resident" | "stub" | "arriving" | "remote" }
  | { tier: "hot" | "warm" | "cold" | "archive" | "smart"; role?: "home" | "source" }
  | { recalling: "true" };

/** Under ["files", id] so every pin, hold and recall mutation refreshes it too. */
export const journeyKey = (fileId: string) => ["files", fileId, "journey"] as const;
/** Under ["shares", id]: share mutations refresh the map. */
export const mapKey = (shareId: string) => ["shares", shareId, "map"] as const;

export function useFileJourney(fileId: string | null) {
  return useQuery({
    queryKey: journeyKey(fileId ?? ""),
    enabled: !!fileId,
    queryFn: () =>
      unwrap(
        api.GET("/v1/files/{file_id}/journey", { params: { path: { file_id: fileId ?? "" } } }),
      ),
  });
}

export function useShareMap(shareId: string) {
  return useQuery({
    queryKey: mapKey(shareId),
    // One pass over the share's files per call (portal-api.md 5.22): keep it a minute.
    staleTime: 60_000,
    queryFn: () =>
      unwrap(api.GET("/v1/shares/{share_id}/map", { params: { path: { share_id: shareId } } })),
  });
}

export function useShareMapFiles(shareId: string, node: MapNode | null) {
  return useInfiniteQuery({
    queryKey: [...mapKey(shareId), "files", node] as const,
    enabled: !!node,
    initialPageParam: null as string | null,
    getNextPageParam: (last: { next_cursor: string | null }) => last.next_cursor,
    queryFn: ({ pageParam }) =>
      unwrap(
        api.GET("/v1/shares/{share_id}/map/files", {
          params: {
            path: { share_id: shareId },
            query: { ...(node ?? {}), ...(pageParam ? { cursor: pageParam } : {}), limit: 50 },
          },
        }),
      ),
  });
}
