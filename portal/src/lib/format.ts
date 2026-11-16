import type { Entry, LogicalTier, Money, RecallState } from "../api/types";

const nf = (options: Intl.NumberFormatOptions) => new Intl.NumberFormat(undefined, options);

/** Decimal units, as the product talks about sizes ("184 GB", "no promise above 10 GB"). */
export function formatBytes(bytes: number | null | undefined): string {
  if (bytes === null || bytes === undefined) return "—";
  if (bytes < 1000) return `${bytes} B`;
  const units = ["kB", "MB", "GB", "TB", "PB"];
  let value = bytes;
  let unit = "B";
  for (const next of units) {
    if (value < 1000) break;
    value /= 1000;
    unit = next;
  }
  const digits = value >= 100 ? 0 : value >= 10 ? 1 : 2;
  return `${nf({ maximumFractionDigits: digits }).format(value)} ${unit}`;
}

export function formatDateTime(iso: string | null | undefined): string {
  if (!iso) return "—";
  return new Date(iso).toLocaleString(undefined, {
    year: "numeric",
    month: "short",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  });
}

/** A date and time short enough for one table line: the year only when it isn't this one. */
export function formatShortDateTime(
  iso: string | null | undefined,
  now: Date = new Date(),
): string {
  if (!iso) return "—";
  const d = new Date(iso);
  return d.toLocaleString(undefined, {
    ...(d.getFullYear() === now.getFullYear() ? {} : { year: "numeric" }),
    month: "short",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  });
}

/** Just the day, for one table line: "Nov 6" (the year only when it isn't this one). */
export function formatShortDay(iso: string | null | undefined, now: Date = new Date()): string {
  if (!iso) return "—";
  const d = new Date(iso);
  return d.toLocaleDateString(undefined, {
    ...(d.getFullYear() === now.getFullYear() ? {} : { year: "numeric" }),
    month: "short",
    day: "numeric",
  });
}

/** A date-only value (YYYY-MM-DD), shown without a time-zone shift. */
/** A duration in seconds, in the largest sensible unit: "45 s", "12 min", "3.5 h". */
export function formatDuration(seconds: number | null | undefined): string {
  if (seconds === null || seconds === undefined) return "—";
  if (seconds < 90) return `${Math.round(seconds)} s`;
  if (seconds < 90 * 60) return `${Math.round(seconds / 60)} min`;
  const hours = seconds / 3600;
  return `${hours < 10 ? hours.toFixed(1) : Math.round(hours)} h`;
}

export function formatDate(value: string | null | undefined): string {
  if (!value) return "—";
  const iso = value.length === 10 ? `${value}T00:00:00Z` : value;
  return new Date(iso).toLocaleDateString(undefined, {
    year: "numeric",
    month: "short",
    day: "numeric",
    timeZone: value.length === 10 ? "UTC" : undefined,
  });
}

const rtf = () => new Intl.RelativeTimeFormat(undefined, { numeric: "auto" });

export function formatRelative(iso: string | null | undefined, now: number = Date.now()): string {
  if (!iso) return "—";
  const seconds = (new Date(iso).getTime() - now) / 1000;
  const abs = Math.abs(seconds);
  if (abs < 60) return rtf().format(Math.round(seconds), "second");
  if (abs < 3600) return rtf().format(Math.round(seconds / 60), "minute");
  if (abs < 86400 * 2) return rtf().format(Math.round(seconds / 3600), "hour");
  return rtf().format(Math.round(seconds / 86400), "day");
}

export function formatMoney(money: Money | null | undefined): string {
  if (!money) return "—";
  const fmt = nf({ style: "currency", currency: money.currency, maximumFractionDigits: 2 });
  const low = fmt.format(Number(money.amount));
  return money.high !== undefined ? `${low} – ${fmt.format(Number(money.high))}` : low;
}

/**
 * Open enums (x-extensible-enum, portal-api.md 1, decision 35) may gain values
 * at any time: a new backend, tier or migration state. Every label lookup falls
 * back to the raw value, so an unknown one is shown as it is, never blank.
 */
function known(labels: Record<string, string>, value: string | null | undefined): string {
  if (value === null || value === undefined || value === "") return "—";
  return Object.hasOwn(labels, value) ? (labels[value] ?? value) : value;
}

const TIER_LABEL: Record<string, string> = {
  hot: "Hot",
  warm: "Cool",
  cold: "Cold",
  archive: "Archive",
  smart: "Smart",
};

export function tierLabel(tier: LogicalTier | null | undefined): string {
  return known(TIER_LABEL, tier);
}

export function isKnownTier(tier: string | null | undefined): boolean {
  return !!tier && Object.hasOwn(TIER_LABEL, tier);
}

export const TIER_ORDER: LogicalTier[] = ["hot", "warm", "cold", "archive"];

export function stateLabel(state: Entry["state"]): string {
  return known(
    {
      landing: "Arriving",
      uploading: "Uploading",
      resident: "On the gateway",
      stub: "In the cloud",
      recalling: "Coming back",
      remote: "In the cloud",
    },
    state,
  );
}

export function priorityLabel(priority: string | null | undefined): string {
  return known({ standard: "Standard", high: "High priority" }, priority);
}

export function backendLabel(kind: string | null | undefined): string {
  return known({ azure_blob: "Azure Blob", s3: "Amazon S3", sim: "Simulated backend" }, kind);
}

export function layoutLabel(layout: string | null | undefined): string {
  return known(
    {
      two_account: "live account (Smart-capable) plus an archive account",
      single_account: "one account, tiered in place",
    },
    layout,
  );
}

export function migrationLabel(state: string | null | undefined): string {
  return known(
    { steady: "Steady", migrating: "Migrating", cut_over: "Cut over", handed_off: "Handed off" },
    state,
  );
}

export function costItemLabel(item: string): string {
  return known(
    {
      storage: "Storage",
      transitions: "Tier changes",
      archive_copy: "Copy to the archive account",
      read_back: "Read back to verify",
      early_deletion: "Early-deletion fees",
      smart_monitoring: "Smart monitoring",
      recall: "Recalls at the assumed rate",
    },
    item,
  );
}

export function accountLabel(account: string | null | undefined): string {
  return known(
    { live: "live account", archive: "archive account", single: "one account" },
    account,
  );
}

export const RECALL_STATE_LABEL: Record<RecallState, string> = {
  requested: "Requested",
  rehydrating: "Rehydrating",
  copying: "Rehydrating",
  downloading: "Downloading",
  available: "Available",
  failed: "Failed",
  cancelled: "Cancelled",
  // The file was replaced or deleted while it was being recalled: not a failure.
  superseded: "Stopped",
};

/** A superseded recall's badge: what happened to the file, when the server says. */
export function supersededLabel(reason: string | null | undefined): string {
  return reason === "replaced"
    ? "File replaced"
    : reason === "deleted"
      ? "File deleted"
      : RECALL_STATE_LABEL.superseded;
}

export const ETA_CONFIDENCE_LABEL = {
  provider_sla: "provider estimate",
  measured: "measured",
  none: "no time promised",
} as const;

export const NEXT_ACTION_LABEL: Record<NonNullable<Entry["next_action"]>, string> = {
  tier_warm: "Moves to Cool",
  tier_cold: "Moves to Cold",
  archive: "Archives",
  stub: "Frees local copy",
  prune_versions: "Prunes old versions",
};

/** `datetime-local` input value (local time, minutes) from an ISO instant. */
export function toLocalInput(date: Date): string {
  const pad = (n: number) => String(n).padStart(2, "0");
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}T${pad(
    date.getHours(),
  )}:${pad(date.getMinutes())}`;
}

export function fromLocalInput(value: string): string | null {
  if (!value) return null;
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? null : date.toISOString();
}

export function addDays(date: Date, days: number): Date {
  return new Date(date.getTime() + days * 86_400_000);
}

export function pluralize(count: number, one: string, many = `${one}s`): string {
  return `${nf({}).format(count)} ${count === 1 ? one : many}`;
}
