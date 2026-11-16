/**
 * Filers and annexes (portal-api.md 5.20; docs/design/annex.md, phase A1).
 * Every enum here is open: unknown values are shown as they come.
 */
import { keepPreviousData, useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api, idem, unwrap } from "../../api/client";
import type { components } from "../../api/schema.gen";

export type Filer = components["schemas"]["Filer"];
export type FilerCreate = components["schemas"]["FilerCreate"];
export type Annex = components["schemas"]["Annex"];
export type AnnexDetail = components["schemas"]["AnnexDetail"];
export type AnnexCreate = components["schemas"]["AnnexCreate"];
export type AnnexPatch = components["schemas"]["AnnexPatch"];
export type AnnexEvent = components["schemas"]["AnnexEvent"];
export type AnnexFile = components["schemas"]["AnnexFile"];
export type AnnexTimeline = components["schemas"]["AnnexTimeline"];
export type AnnexProblems = components["schemas"]["AnnexProblems"];
export type AnnexSettings = components["schemas"]["AnnexSettings"];
export type AnnexDefaults = components["schemas"]["AnnexDefaults"];

export const annexKeys = {
  filers: ["filers"] as const,
  annexes: ["annexes"] as const,
  annex: (id: string) => ["annexes", id] as const,
  events: (id: string, kind: string) => ["annexes", id, "events", kind] as const,
  files: (id: string, state: string, path: string) =>
    ["annexes", id, "files", state, path] as const,
  timeline: (id: string, days: number) => ["annexes", id, "timeline", days] as const,
  problems: (id: string) => ["annexes", id, "problems"] as const,
  defaults: ["annex-defaults"] as const,
};

export function useFilers(enabled = true) {
  return useQuery({
    queryKey: annexKeys.filers,
    enabled,
    queryFn: () => unwrap(api.GET("/v1/filers")),
  });
}

/** Decision 65: answer whether the filer's share or export limits access (ArchiveAdmin). */
export function useAnswerFilerAccess() {
  const client = useQueryClient();
  return useMutation({
    mutationFn: async ({
      filerId,
      access,
    }: {
      filerId: string;
      access: { limited: boolean; groups?: string[] };
    }) => {
      const got = await api.GET("/v1/filers/{filer_id}", {
        params: { path: { filer_id: filerId } },
      });
      const etag = got.response.headers.get("ETag") ?? "";
      return unwrap(
        api.PATCH("/v1/filers/{filer_id}", {
          params: { path: { filer_id: filerId }, header: { ...idem(), "If-Match": etag } },
          body: { access },
        }),
      );
    },
    onSuccess: () => {
      void client.invalidateQueries({ queryKey: annexKeys.filers });
      void client.invalidateQueries({ queryKey: annexKeys.annexes });
    },
  });
}

/** Decision 64: acknowledge that the filer's NAS shows no ACLs (ArchiveAdmin). */
export function useAcknowledgeAclsHidden() {
  const client = useQueryClient();
  return useMutation({
    mutationFn: async ({ filerId }: { filerId: string }) => {
      const got = await api.GET("/v1/filers/{filer_id}", {
        params: { path: { filer_id: filerId } },
      });
      const etag = got.response.headers.get("ETag") ?? "";
      return unwrap(
        api.PATCH("/v1/filers/{filer_id}", {
          params: { path: { filer_id: filerId }, header: { ...idem(), "If-Match": etag } },
          body: { acls_hidden_acknowledged: true },
        }),
      );
    },
    onSuccess: () => {
      void client.invalidateQueries({ queryKey: annexKeys.filers });
      void client.invalidateQueries({ queryKey: annexKeys.annexes });
    },
  });
}

export function useAddFiler() {
  const client = useQueryClient();
  return useMutation({
    mutationFn: (body: FilerCreate) =>
      unwrap(api.POST("/v1/filers", { params: { header: idem() }, body })),
    onSuccess: () => void client.invalidateQueries({ queryKey: annexKeys.filers }),
  });
}

export function useAnnexes(enabled = true) {
  return useQuery({
    queryKey: annexKeys.annexes,
    enabled,
    queryFn: () => unwrap(api.GET("/v1/annexes")),
  });
}

export function useAnnex(id: string) {
  return useQuery({
    queryKey: annexKeys.annex(id),
    refetchInterval: 30_000,
    queryFn: async () => {
      const r = await api.GET("/v1/annexes/{annex_id}", { params: { path: { annex_id: id } } });
      const body = await unwrap(Promise.resolve(r));
      return { annex: body, etag: r.response.headers.get("ETag") ?? "" };
    },
  });
}

export function useAddAnnex() {
  const client = useQueryClient();
  return useMutation({
    mutationFn: (body: AnnexCreate) =>
      unwrap(api.POST("/v1/annexes", { params: { header: idem() }, body })),
    onSuccess: () => void client.invalidateQueries({ queryKey: annexKeys.annexes }),
  });
}

export function usePatchAnnex(id: string) {
  const client = useQueryClient();
  return useMutation({
    mutationFn: ({ body, etag }: { body: AnnexPatch; etag: string }) =>
      unwrap(
        api.PATCH("/v1/annexes/{annex_id}", {
          params: { path: { annex_id: id }, header: { "If-Match": etag } },
          body,
        }),
      ),
    onSuccess: () => void client.invalidateQueries({ queryKey: annexKeys.annexes }),
  });
}

export function useAnnexEvents(id: string, kind: string) {
  return useQuery({
    queryKey: annexKeys.events(id, kind),
    placeholderData: keepPreviousData,
    queryFn: () =>
      unwrap(
        api.GET("/v1/annexes/{annex_id}/events", {
          params: { path: { annex_id: id }, query: { limit: 50, ...(kind ? { kind } : {}) } },
        }),
      ),
  });
}

export function useAnnexFiles(id: string, state: string, path: string) {
  return useQuery({
    queryKey: annexKeys.files(id, state, path),
    placeholderData: keepPreviousData,
    queryFn: () =>
      unwrap(
        api.GET("/v1/annexes/{annex_id}/files", {
          params: {
            path: { annex_id: id },
            query: {
              limit: 100,
              ...(state ? { state: state as "present" | "gone" | "purged" } : {}),
              ...(path ? { path } : {}),
            },
          },
        }),
      ),
  });
}

export function useAnnexTimeline(id: string, days: number) {
  return useQuery({
    queryKey: annexKeys.timeline(id, days),
    queryFn: () =>
      unwrap(
        api.GET("/v1/annexes/{annex_id}/timeline", {
          params: { path: { annex_id: id }, query: { days } },
        }),
      ),
  });
}

export function useAnnexProblems(id: string) {
  return useQuery({
    queryKey: annexKeys.problems(id),
    queryFn: () =>
      unwrap(api.GET("/v1/annexes/{annex_id}/problems", { params: { path: { annex_id: id } } })),
  });
}

export function usePurgeAnnexFile(id: string) {
  const client = useQueryClient();
  return useMutation({
    mutationFn: (fileId: string) =>
      unwrap(
        api.POST("/v1/annexes/{annex_id}/files/{file_id}/purge", {
          params: { path: { annex_id: id, file_id: fileId }, header: idem() },
        }),
      ),
    onSuccess: () => void client.invalidateQueries({ queryKey: annexKeys.annex(id) }),
  });
}

/** A copy Azure keeps refusing (#81's follow-up): stop or resume copying it again, or
 * acknowledge the refusals. ArchiveAdmin, with a fresh sign-in. */
export type CopyRefusedAction = "copy-stop" | "copy-resume" | "copy-refused-ack";

export function useCopyRefusedAction(id: string) {
  const client = useQueryClient();
  return useMutation({
    mutationFn: ({ fileId, action }: { fileId: string; action: CopyRefusedAction }) => {
      const params = { path: { annex_id: id, file_id: fileId }, header: idem() };
      switch (action) {
        case "copy-stop":
          return unwrap(api.POST("/v1/annexes/{annex_id}/files/{file_id}/copy-stop", { params }));
        case "copy-resume":
          return unwrap(api.POST("/v1/annexes/{annex_id}/files/{file_id}/copy-resume", { params }));
        case "copy-refused-ack":
          return unwrap(
            api.POST("/v1/annexes/{annex_id}/files/{file_id}/copy-refused-ack", { params }),
          );
      }
    },
    onSuccess: () => void client.invalidateQueries({ queryKey: annexKeys.annex(id) }),
  });
}

export function useAnnexDefaults(enabled = true) {
  return useQuery({
    queryKey: annexKeys.defaults,
    enabled,
    queryFn: () => unwrap(api.GET("/v1/settings/annex-defaults")),
  });
}

/** Open enums: labels for the known values; anything else is shown raw. */
export const STATE_LABEL: Record<string, string> = {
  registered: "Starting",
  baseline: "First scan",
  active: "Copying",
  paused: "Paused",
  retired: "Retired",
};

export const COPY_LABEL: Record<string, { label: string; badge: string }> = {
  pending: { label: "Waiting", badge: "badge-outline" },
  copied: { label: "Copied, checking", badge: "badge-info" },
  verified: { label: "Verified", badge: "badge-ok" },
  changed: { label: "Changed, copying again", badge: "badge-warn" },
  purged: { label: "Purged", badge: "badge-danger" },
};

export const NAS_LABEL: Record<string, string> = {
  present: "On the NAS",
  gone: "Deleted on the NAS",
  removing: "Being removed",
  removed: "Removed by OmniFile",
  returned: "Returned",
};

export const DRIFT_LABEL: Record<string, string> = {
  missed_arrival: "A file the scans missed",
  missed_departure: "A delete the scans missed",
  changed_no_event: "A change the scans missed",
  copy_mismatch: "A copy that no longer matches",
};

export const PROTOCOL_LABEL: Record<string, string> = {
  smb: "SMB",
  nfs3: "NFSv3",
  nfs4: "NFSv4",
};

export const VENDOR_LABEL: Record<string, string> = {
  generic: "NFS/SMB server",
  ontap: "NetApp ONTAP",
  powerscale: "Dell PowerScale",
};

export function label(map: Record<string, string>, value: string | null | undefined): string {
  if (!value) return "—";
  return map[value] ?? value;
}
