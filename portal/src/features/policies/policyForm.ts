import type { PolicyBody } from "../../api/types";

/**
 * The editor's form state, and the mapping to and from catalog.policy.body
 * (portal-api.md 5.7). Days are strings while being typed; the server checks
 * them (archive_day required, days increasing, Smart has no Cool or Cold days).
 */
export interface PolicyForm {
  mode: "scheduled" | "smart";
  clock: "last_read" | "written";
  useWarm: boolean;
  warmDay: string;
  useCold: boolean;
  coldDay: string;
  archiveDay: string;
  stubAt: "archive" | "leaves_hot" | "never";
  residentMb: string;
  restubAfterHours: string;
  notifyRequester: boolean;
  notifyOwners: boolean;
  watchersSid: string | null;
  watchersName: string | null;
  emailFallback: boolean;
}

export const PRESETS = [
  {
    id: "gentle",
    label: "Gentle",
    hint: "60 · 180 · 730 days",
    warm: 60,
    cold: 180,
    archive: 730,
  },
  {
    id: "balanced",
    label: "Balanced",
    hint: "30 · 120 · 365 days",
    warm: 30,
    cold: 120,
    archive: 365,
  },
  {
    id: "lean",
    label: "Lean",
    hint: "14 · 60 · 180 days",
    warm: 14,
    cold: 60,
    archive: 180,
  },
] as const;

export const DEFAULT_BODY: PolicyBody = {
  mode: "scheduled",
  clock: "last_read",
  warm_day: 30,
  cold_day: 120,
  archive_day: 365,
  stub_at: "archive",
  resident_mb: 4,
  restub_after_hours: 24,
  notify: { requester: true, owners: false, watchers_group_sid: null, email_fallback: true },
};

const str = (n: number | null | undefined, fallback: number) => String(n ?? fallback);

export function isM1Policy(body: PolicyBody | undefined): boolean {
  return !!body && body.archive_day === undefined;
}

export function bodyToForm(body: PolicyBody | undefined): PolicyForm {
  const b = body && !isM1Policy(body) ? body : DEFAULT_BODY;
  const smart = b.mode === "smart";
  return {
    mode: smart ? "smart" : "scheduled",
    clock: b.clock ?? "last_read",
    useWarm: smart ? true : b.warm_day !== null && b.warm_day !== undefined,
    warmDay: str(b.warm_day, 30),
    useCold: smart ? true : b.cold_day !== null && b.cold_day !== undefined,
    coldDay: str(b.cold_day, 120),
    archiveDay: str(b.archive_day, 365),
    stubAt: b.stub_at ?? "archive",
    residentMb: b.resident_mb === null || b.resident_mb === undefined ? "" : String(b.resident_mb),
    restubAfterHours: str(b.restub_after_hours, 24),
    notifyRequester: b.notify?.requester ?? true,
    notifyOwners: b.notify?.owners ?? false,
    watchersSid: b.notify?.watchers_group_sid ?? null,
    watchersName: null,
    emailFallback: b.notify?.email_fallback ?? true,
  };
}

function int(value: string): number | undefined {
  if (value.trim() === "") return undefined;
  const n = Number(value);
  return Number.isFinite(n) ? Math.trunc(n) : undefined;
}

function num(value: string): number | undefined {
  if (value.trim() === "") return undefined;
  const n = Number(value);
  return Number.isFinite(n) ? n : undefined;
}

export function formToBody(form: PolicyForm): PolicyBody {
  const smart = form.mode === "smart";
  const body: PolicyBody = {
    mode: form.mode,
    clock: smart ? "written" : form.clock,
    stub_at: form.stubAt,
    resident_mb: num(form.residentMb) ?? null,
    restub_after_hours: num(form.restubAfterHours) ?? 24,
    notify: {
      requester: form.notifyRequester,
      owners: form.notifyOwners,
      watchers_group_sid: form.watchersSid,
      email_fallback: form.emailFallback,
    },
  };
  if (!smart) {
    body.warm_day = form.useWarm ? (int(form.warmDay) ?? null) : null;
    body.cold_day = form.useCold ? (int(form.coldDay) ?? null) : null;
  }
  const archive = int(form.archiveDay);
  if (archive !== undefined) body.archive_day = archive;
  return body;
}

const LABELS: Record<string, string> = {
  mode: "How files move",
  clock: "Days count from",
  warm_day: "Cool after (days)",
  cold_day: "Cold after (days)",
  archive_day: "Archive after (days)",
  stub_at: "Free the edge copy",
  resident_mb: "MB kept on the edge",
  restub_after_hours: "Free again after a recall (hours)",
  "notify.requester": "Tell the person who opened it",
  "notify.owners": "Tell the share's Owners",
  "notify.watchers_group_sid": "Tell a watchers group",
  "notify.email_fallback": "Email when Teams can't be reached",
  stub_after_minutes: "Stub after (minutes, M1)",
};

export function fieldLabel(field: string): string {
  return LABELS[field] ?? field;
}

function flatten(body: PolicyBody | undefined): Record<string, unknown> {
  if (!body) return {};
  const { notify, ...rest } = body;
  const out: Record<string, unknown> = { ...rest };
  for (const [key, value] of Object.entries(notify ?? {})) out[`notify.${key}`] = value;
  return out;
}

export interface FieldChange {
  field: string;
  from: unknown;
  to: unknown;
}

export function diffBodies(from: PolicyBody | undefined, to: PolicyBody): FieldChange[] {
  const a = flatten(from);
  const b = flatten(to);
  const keys = [...new Set([...Object.keys(a), ...Object.keys(b)])];
  return keys
    .filter((k) => JSON.stringify(a[k] ?? null) !== JSON.stringify(b[k] ?? null))
    .map((field) => ({ field, from: a[field] ?? null, to: b[field] ?? null }));
}

export function showValue(value: unknown): string {
  if (value === null || value === undefined) return "—";
  if (value === true) return "Yes";
  if (value === false) return "No";
  if (value === "last_read") return "last read";
  if (value === "written") return "write";
  if (value === "leaves_hot") return "when it leaves Hot";
  if (value === "archive") return "at Archive";
  if (value === "never") return "never";
  return typeof value === "string" || typeof value === "number"
    ? String(value)
    : JSON.stringify(value);
}

/** Where a check's `field` lives in the editor, for "Go to the setting". */
export function fieldElementId(field: string, shareId?: string | null): string {
  if ((field === "on_open" || field === "wait_threshold_minutes") && shareId) {
    return field === "on_open" ? `f-on_open-${shareId}` : `f-threshold-${shareId}`;
  }
  return `f-${field.replace(/\./g, "-")}`;
}

/** One-line summary of a policy, for lists. */
export function policySummary(body: PolicyBody | undefined): string {
  if (!body) return "No policy yet";
  if (isM1Policy(body)) return `M1 policy: stub after ${body.stub_after_minutes ?? "?"} minutes`;
  if (body.mode === "smart") return `Smart, archive after ${body.archive_day} days from the write`;
  const parts = [];
  if (body.warm_day != null) parts.push(`Cool ${body.warm_day} d`);
  if (body.cold_day != null) parts.push(`Cold ${body.cold_day} d`);
  parts.push(`Archive ${body.archive_day} d`);
  return `Scheduled from the ${body.clock === "written" ? "write" : "last read"}: ${parts.join(" · ")}`;
}
