/** Plain words for the console's open enums; an unknown value is shown raw. */

function known(labels: Record<string, string>, value: string | null | undefined): string {
  if (value === null || value === undefined || value === "") return "—";
  return Object.hasOwn(labels, value) ? (labels[value] ?? value) : value;
}

export function healthLabel(health: string | null | undefined): string {
  return known(
    {
      ok: "Healthy",
      attention: "Needs attention",
      failing: "Failing",
      done: "Done",
      cancelled: "Cancelled",
    },
    health,
  );
}

/** The badge class for a health value (unknown values are neutral). */
export function healthBadge(health: string): string {
  const tone: Record<string, string> = {
    ok: "badge-ok",
    attention: "badge-warn",
    failing: "badge-danger",
    done: "badge-info",
  };
  return `badge ${tone[health] ?? "badge-outline"}`;
}

export function waveStateLabel(state: string | null | undefined): string {
  return known(
    {
      planned: "Planned",
      waiting: "Waiting",
      blocked: "Blocked",
      started: "Running",
      verified: "Verified",
      cut_over: "Cut over",
      failed: "Failed",
      cancelled: "Cancelled",
    },
    state,
  );
}

export function planStateLabel(state: string | null | undefined): string {
  return known(
    {
      proposed: "Proposed, waiting for approval",
      approved: "Approved",
      rejected: "Rejected",
      done: "Done",
      cancelled: "Cancelled",
    },
    state,
  );
}

export function attentionLabel(kind: string): string {
  return known(
    {
      access: "Access or network",
      verify_failed: "Copies failed their check",
      check_given_up: "Checks gave up",
      copy_stranded: "Copies nothing will check",
      no_checksum: "Copies with no checksum",
      run_failed: "Copy run failed",
      restore_expiring: "Restored copies about to expire",
      failed: "Migration stopped",
      agent_offline: "Agent offline",
      seeding: "Still cataloguing",
    },
    kind,
  );
}

export function agentStatusBadge(status: string | null, stale: boolean): string {
  if (stale) return "badge badge-warn";
  return status === "Online" ? "badge badge-ok" : "badge badge-danger";
}

const UNITS = ["B", "kB", "MB", "GB", "TB", "PB"];

/** Bytes per second, in decimal units. */
export function rateLabel(bytesPerS: number): string {
  let v = bytesPerS;
  let u = 0;
  while (v >= 1000 && u < UNITS.length - 1) {
    v /= 1000;
    u += 1;
  }
  return `${v >= 100 ? Math.round(v) : v.toFixed(1)} ${UNITS[u] ?? "B"}/s`;
}

/** A UTC HH:MM window, as people read it. */
export function windowLabel(w: { start: string; end: string } | null): string {
  if (!w) return "Any time";
  return `${w.start}–${w.end} UTC${w.end < w.start ? " (overnight)" : ""}`;
}

/** A time in UTC, as plans are set: "2026-11-05 22:00 UTC". */
export function formatUtc(iso: string | null | undefined): string {
  if (!iso) return "—";
  return `${new Date(iso).toISOString().slice(0, 16).replace("T", " ")} UTC`;
}

/** A `datetime-local` value read and written as UTC (the field is labelled so). */
export function toUtcInput(iso: string): string {
  return new Date(iso).toISOString().slice(0, 16);
}

export function fromUtcInput(value: string): string | null {
  if (!/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}$/.test(value)) return null;
  const t = Date.parse(`${value}:00Z`);
  return Number.isNaN(t) ? null : new Date(t).toISOString();
}

/** Why a migration may be stuck with no error: the worker's count, in the page's words. */
export function stuckNotes(stuck?: {
  given_up: number;
  stranded: number;
  no_checksum: number;
}): string[] {
  const copies = (n: number) => (n === 1 ? "1 copy" : `${n.toLocaleString()} copies`);
  const notes: string[] = [];
  if (stuck?.given_up) {
    notes.push(
      `The check of ${copies(stuck.given_up)} kept failing and stopped. An administrator can send them again; a new copy run won't help.`,
    );
  }
  if (stuck?.stranded) {
    notes.push(
      `Copies waiting to be checked with no check queued: ${stuck.stranded.toLocaleString()}. A new copy run won't help.`,
    );
  }
  if (stuck?.no_checksum) {
    notes.push(
      `Copies with nothing to be checked against, as S3 has no checksum on record for them: ${stuck.no_checksum.toLocaleString()}. Exclude them to cut over the rest; they stay on S3.`,
    );
  }
  return notes;
}
