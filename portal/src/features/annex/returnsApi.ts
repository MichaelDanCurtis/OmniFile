/**
 * Getting an annexed file back (portal-api.md 5.25; annex.md 4.4-4.6, phase A3.1).
 * Return rules are `recall_completed` rules in the annex's rule set; the service
 * evaluates them. Every enum here is open.
 */
import { useMutation, useQuery } from "@tanstack/react-query";
import { api, idem, unwrap } from "../../api/client";
import type { components } from "../../api/schema.gen";
import type { Rule } from "./rulesApi";

export type ReturnLocation = components["schemas"]["ReturnLocation"];
export type ReturnTargets = components["schemas"]["ReturnTargets"];
export type ReturnTarget = ReturnTargets["items"][number];
export type Placement = components["schemas"]["AnnexPlacement"];
export type PlacementCreate = components["schemas"]["PlacementCreate"];
export type ReturnPreview = components["schemas"]["AnnexReturnPreview"];

export const RETURN_TRIGGER = "recall_completed";

/** One return rule as the builder edits it. */
export interface ReturnRule {
  id: string;
  label?: string;
  trigger: typeof RETURN_TRIGGER;
  conditions: {
    path?: string[];
    extension?: string[];
    size_gte?: number;
    size_lt?: number;
    owner_sids?: string[];
    group_sids?: string[];
    requester_in?: string[];
  };
  actions: ReturnAction[];
}

export type Conflict = "same_content" | "restored_name" | "overwrite_keep_version";

export type ReturnAction =
  | { exclude: true }
  | { place_at: ReturnLocation }
  | { on_conflict: Conflict }
  | { hold_days: number }
  | { notify: (string | { group_sid: string })[] }
  | { let_requester_choose: ReturnLocation[] };

export const ORIGIN: ReturnLocation = { kind: "nas_folder", origin: true };

export const CONFLICT_LABEL: Record<string, string> = {
  restored_name: "Keep both: name it -restored",
  same_content: "Leave it if the same file is already there, else -restored",
  overwrite_keep_version: "Replace it, keeping the old one as a version",
};

/** A3.4 (decisions 82 to 84): what replacing on a NAS means, in the rule builder. */
export const REPLACE_HINT =
  "On the NAS, only the file's own name in its original folder is replaced, and only " +
  "when the file there is exactly the copy OmniFile verified; anywhere else it takes a " +
  "-restored name. The old file is hidden first and the name stays empty for the grace " +
  "period, then the returning file takes it and the old one is deleted (it stays a " +
  "version). A change to the old file in that time keeps it: under its own name, or as " +
  "<name>-replaced-<time> if the returning file has the name by then.";

/** What became of a replace at the origin, for the placement's page (A3.4). */
export function replaceSummary(p: Placement): string | null {
  const r = p.replace;
  if (!r) return null;
  const v = r.replaced_version_id ? `version ${r.replaced_version_id}` : "a version";
  switch (r.state) {
    case "replaced":
      return `Replaced; the old content is ${v}.`;
    case "kept_aside":
      return `Placed; the file that was there changed, so it is kept as ${r.kept_as ?? "a -replaced- name"} (${v} is its last copy).`;
    case "hidden":
      return `The file there is hidden as ${r.hidden_name ?? "a hidden name"} for the grace period.`;
    case "put_back":
    case "refused":
      return `Not replaced (${r.refused ?? "refused"}): the file there was left as it is.`;
    case "same":
      return "The file there already has these bytes.";
    case "gone":
      return "The file that was there was moved or deleted by someone first; OmniFile deleted nothing.";
    default:
      return null;
  }
}

export function isReturn(r: { trigger: string }): boolean {
  return r.trigger === RETURN_TRIGGER;
}

export function get(r: ReturnRule, key: string): unknown {
  for (const a of r.actions) if (key in a) return (a as Record<string, unknown>)[key];
  return undefined;
}

/** "Back where it came from", "Into a restore folder", "Into an OmniFile share"
 * (annex.md 4.4), as the service's templates are. */
export function returnTemplate(
  name: "origin" | "restore_folder" | "share",
  location?: ReturnLocation,
): ReturnRule[] {
  const notify: ReturnAction = { notify: ["requester"] };
  if (name === "origin") {
    return [
      {
        id: "back",
        trigger: RETURN_TRIGGER,
        conditions: {},
        actions: [
          { place_at: ORIGIN },
          { on_conflict: "restored_name" },
          { hold_days: 14 },
          notify,
        ],
      },
    ];
  }
  const loc = location ?? { kind: name === "share" ? "share_folder" : "nas_folder", path: "" };
  return [
    {
      id: name === "share" ? "to_share" : "restore",
      trigger: RETURN_TRIGGER,
      conditions: {},
      actions: [
        { place_at: loc },
        { on_conflict: name === "share" ? "overwrite_keep_version" : "restored_name" },
        notify,
      ],
    },
  ];
}

export function asRules(back: ReturnRule[]): Rule[] {
  return back as unknown as Rule[];
}

export function useReturnTargets(fileId: string | null) {
  return useQuery({
    queryKey: ["files", fileId, "return-targets"],
    enabled: fileId !== null,
    queryFn: () =>
      unwrap(
        api.GET("/v1/files/{file_id}/return-targets", {
          params: { path: { file_id: fileId ?? "" } },
        }),
      ),
  });
}

export function useRequestPlacement() {
  return useMutation({
    mutationFn: (body: PlacementCreate) =>
      unwrap(api.POST("/v1/placements", { params: { header: idem() }, body })),
  });
}

/** A placement, polled until it ends. */
export function usePlacement(placementId: string | null) {
  return useQuery({
    queryKey: ["placements", placementId],
    enabled: placementId !== null,
    refetchInterval: (q) =>
      q.state.data && ["placed", "failed", "cancelled"].includes(q.state.data.state) ? false : 5000,
    queryFn: () =>
      unwrap(
        api.GET("/v1/placements/{placement_id}", {
          params: { path: { placement_id: placementId ?? "" } },
        }),
      ),
  });
}

export function useReturnPreview(annexId: string) {
  return useMutation({
    mutationFn: (body: unknown) =>
      unwrap(
        api.POST("/v1/annexes/{annex_id}/return-preview", {
          params: { path: { annex_id: annexId }, header: idem() },
          body: { body } as components["schemas"]["AnnexDryRunCreate"],
        }),
      ),
  });
}
