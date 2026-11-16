/**
 * Plain words for the migration engine's states and choices
 * (docs/design/migration-engine.md). Every field here is an open enum
 * (portal-api.md 1, decision 35): an unknown value is shown raw, never blank.
 * User-facing text says "API" for an external copier, never a product name.
 */

function known(labels: Record<string, string>, value: string | null | undefined): string {
  if (value === null || value === undefined || value === "") return "—";
  return Object.hasOwn(labels, value) ? (labels[value] ?? value) : value;
}

export function migrationStateLabel(state: string | null | undefined): string {
  return known(
    {
      pending: "Waiting to start",
      copying: "Copying",
      verifying: "Verifying copies",
      verified: "Every file verified",
      cut_over: "Done: served from the new backend",
      failed: "Failed",
      cancelled: "Cancelled",
    },
    state,
  );
}

export function sourceStateLabel(state: string | null | undefined): string {
  return known(
    { registered: "Registered", seeding: "Cataloguing", seeded: "Catalogued", failed: "Failed" },
    state,
  );
}

/** Who copies: OmniFile's Storage Mover jobs, or an external copier through the API. */
export function copierLabel(kind: string | null | undefined): string {
  return known(
    { storage_mover: "Azure Storage Mover", external: "API (an external copier)" },
    kind,
  );
}

export function cutoverLabel(cutover: string | null | undefined): string {
  return known(
    {
      auto: "Automatic, once every file is verified",
      manual: "Manual: an ArchiveAdmin presses Cut over",
    },
    cutover,
  );
}

export function modeLabel(mode: string | null | undefined): string {
  return known({ serve: "Serve from Azure behind the same share", hand_off: "Hand off" }, mode);
}

export function runStateLabel(state: string | null | undefined): string {
  return known({ running: "Running", succeeded: "Succeeded", failed: "Failed" }, state);
}

export function proofLabel(proof: string | null | undefined): string {
  return known({ proven: "proven", claimed: "claimed by the client", unknown: "not known" }, proof);
}

/** Who started it: `app:<client id>` is an API client, `user:<id>` a person. */
export function actorLabel(actor: string): string {
  if (actor.startsWith("app:")) return `API client ${actor.slice(4)}`;
  if (actor.startsWith("user:")) return "An ArchiveAdmin, in the portal";
  return actor;
}

/** The lifecycle, in order (portal-api.md 5.17). */
export const LIFECYCLE = [
  { state: "pending", name: "Pending", hint: "Queued for the worker" },
  { state: "copying", name: "Copying", hint: "Copies land in Azure" },
  { state: "verifying", name: "Verifying", hint: "Each copy is checked byte for byte" },
  { state: "verified", name: "Verified", hint: "Every file matches" },
  { state: "cut_over", name: "Cut over", hint: "Served from Azure" },
] as const;

export type StepStatus = "done" | "current" | "failed" | "pending";

/**
 * Each lifecycle step's status for a migration. A failed or cancelled one marks
 * the step it stopped at: the furthest it reached, judged by its timestamps.
 */
export function lifecycle(m: {
  state: string;
  verified_at: string | null;
  cut_over_at: string | null;
  files: { verified: number };
  copier: { last_run: unknown };
}): StepStatus[] {
  const order: string[] = LIFECYCLE.map((s) => s.state);
  let at = order.indexOf(m.state);
  const stopped = at === -1;
  if (stopped) {
    // failed, cancelled, or a state this portal doesn't know.
    at = m.cut_over_at
      ? 4
      : m.verified_at
        ? 3
        : m.files.verified > 0
          ? 2
          : m.copier.last_run
            ? 1
            : 0;
  }
  return order.map((_, i) => {
    if (i < at) return "done";
    if (i > at) return "pending";
    if (m.state === "cut_over") return "done";
    return stopped ? "failed" : "current";
  });
}
