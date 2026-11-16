import { useId, useRef, useState, type KeyboardEvent, type PointerEvent } from "react";
import type { Check, PolicyPreview } from "../../../api/types";
import { AlertIcon, ErrorIcon, InfoIcon } from "../../../components/icons";
import { tierLabel } from "../../../lib/format";
import "../../files/visual/visual.css";
import { TierLegend, className, useReducedMotion } from "../../files/visual/tiers";
import type { PolicyForm } from "../policyForm";

type DayKey = "warmDay" | "coldDay" | "archiveDay";
type Field = "warm_day" | "cold_day" | "archive_day";

interface Boundary {
  key: DayKey;
  field: Field;
  tier: "warm" | "cold" | "archive";
  value: number;
}

interface Segment {
  tier: string;
  from: number;
  to: number | null;
  endsAt: Field | null;
}

/** Days a handle can reach: a policy longer than ten years is typed, not dragged. */
export const MAX_DAY = 3650;
const FIELDS: Field[] = ["warm_day", "cold_day", "archive_day"];

const num = (s: string): number | null => {
  if (s.trim() === "") return null;
  const n = Number(s);
  return Number.isFinite(n) ? Math.trunc(n) : null;
};

export function boundariesOf(form: PolicyForm): Boundary[] {
  const out: Boundary[] = [];
  const add = (key: DayKey, field: Field, tier: Boundary["tier"], on: boolean) => {
    const v = num(form[key]);
    if (on && v !== null) out.push({ key, field, tier, value: v });
  };
  if (form.mode === "scheduled") {
    add("warmDay", "warm_day", "warm", form.useWarm);
    add("coldDay", "cold_day", "cold", form.useCold);
  }
  add("archiveDay", "archive_day", "archive", true);
  return out;
}

function segmentsOf(form: PolicyForm, bounds: Boundary[]): Segment[] {
  const segs: Segment[] = [];
  let tier = form.mode === "smart" ? "smart" : "hot";
  let from = 0;
  for (const b of bounds) {
    segs.push({ tier, from, to: b.value, endsAt: b.field });
    tier = b.tier;
    from = b.value;
  }
  segs.push({ tier, from, to: null, endsAt: null });
  return segs;
}

/** Drawn days: at least 400 (as the preview's strata), with room for Archive after the last day. */
function horizonFor(bounds: Boundary[]): number {
  const last = Math.max(0, ...bounds.map((b) => b.value));
  return Math.max(400, Math.ceil((last * 1.25) / 50) * 50);
}

/** Checks drawn on the timeline: errors and minimum-duration warnings on a day field. */
const ON_TRACK = (c: Check) =>
  !!c.field &&
  FIELDS.includes(c.field as Field) &&
  (c.severity === "error" || c.code === "minimum_duration");

const pct = (day: number, horizon: number) =>
  `${Math.max(0, Math.min(100, (day / horizon) * 100))}%`;

/**
 * The tier schedule as a timeline whose boundaries can be dragged. It edits
 * the same form state as the day inputs next to it (which stay the typed
 * alternative), so the live preview and its checks follow every move. Each
 * handle is a slider: arrows move a day, Shift+arrows ten, Page Up/Down thirty,
 * Home and End to the ends of its range. Checks placed on a day field (such as
 * a tier left before its minimum) are drawn on that boundary.
 */
export function PolicyTimeline({
  form,
  onDays,
  preview,
}: {
  form: PolicyForm;
  onDays: (patch: Partial<Record<DayKey, string>>) => void;
  preview: PolicyPreview | undefined;
}) {
  const reduced = useReducedMotion();
  const track = useRef<HTMLDivElement>(null);
  const [dragging, setDragging] = useState<DayKey | null>(null);
  const [frozen, setFrozen] = useState<number | null>(null);
  const noteId = useId();
  const bounds = boundariesOf(form);
  const segs = segmentsOf(form, bounds);
  const horizon = frozen ?? horizonFor(bounds);
  const smart = form.mode === "smart";
  const clock = smart || form.clock === "written" ? "the write" : "the last read";
  const checks = (preview?.checks ?? []).filter(ON_TRACK);
  const shortAt = new Set(
    checks.filter((c) => c.code === "minimum_duration").map((c) => c.field as Field),
  );
  const native = (tier: string) =>
    preview?.timeline.segments.find((s) => s.logical_tier === tier)?.native_class ?? null;
  const checkOf = (field: Field): Check["severity"] | undefined => {
    const mine = checks.filter((c) => c.field === field);
    if (mine.some((c) => c.severity === "error")) return "error";
    if (mine.some((c) => c.severity === "warning")) return "warning";
    return undefined;
  };

  const range = (i: number): [number, number] => {
    const prev = bounds[i - 1]?.value;
    const next = bounds[i + 1]?.value;
    const lo = prev === undefined ? 0 : prev + 1;
    const hi = next === undefined ? MAX_DAY : next - 1;
    // Never clamp a value the handle didn't set: a typed day outside the range
    // (5000, or days out of order) stays put, and the handle moves from there.
    const v = bounds[i]?.value ?? lo;
    return [Math.min(lo, v), Math.max(hi, v)];
  };
  const move = (i: number, day: number) => {
    const b = bounds[i];
    if (!b) return;
    const [lo, hi] = range(i);
    const v = Math.max(lo, Math.min(hi, Math.round(day)));
    if (v !== b.value) onDays({ [b.key]: String(v) });
  };

  const onKey = (i: number) => (e: KeyboardEvent<HTMLDivElement>) => {
    const b = bounds[i];
    if (!b) return;
    const [lo, hi] = range(i);
    const step = e.shiftKey ? 10 : 1;
    const to: Record<string, number> = {
      ArrowLeft: b.value - step,
      ArrowDown: b.value - step,
      ArrowRight: b.value + step,
      ArrowUp: b.value + step,
      PageDown: b.value - 30,
      PageUp: b.value + 30,
      Home: lo,
      End: hi,
    };
    const day = to[e.key];
    if (day === undefined) return;
    e.preventDefault();
    move(i, day);
  };

  const dayAt = (clientX: number, h: number) => {
    const rect = track.current?.getBoundingClientRect();
    if (!rect || rect.width === 0) return null;
    return ((clientX - rect.left) / rect.width) * h;
  };
  const onDown = (i: number) => (e: PointerEvent<HTMLDivElement>) => {
    const b = bounds[i];
    if (!b) return;
    e.preventDefault();
    try {
      e.currentTarget.setPointerCapture(e.pointerId);
    } catch {
      // no capture (an old browser, or a synthetic event): moves still arrive on the handle
    }
    e.currentTarget.focus();
    setDragging(b.key);
    setFrozen(horizon); // the scale holds still while dragging
  };
  const onMove = (i: number) => (e: PointerEvent<HTMLDivElement>) => {
    if (dragging !== bounds[i]?.key) return;
    const day = dayAt(e.clientX, horizon);
    if (day !== null) move(i, day);
  };
  const onUp = (e: PointerEvent<HTMLDivElement>) => {
    try {
      e.currentTarget.releasePointerCapture(e.pointerId);
    } catch {
      // never captured
    }
    setDragging(null);
    setFrozen(null);
  };

  const ticks = [0, ...bounds.map((b) => b.value), ...(horizon > 365 ? [365] : [])]
    .filter((d, i, all) => d >= 0 && d <= horizon && all.indexOf(d) === i)
    .sort((a, b) => a - b);
  const spaced: number[] = [];
  for (const d of ticks) {
    const prev = spaced.at(-1);
    if (prev === undefined || d - prev > horizon * 0.07) spaced.push(d);
    else if (bounds.some((b) => b.value === d)) spaced[spaced.length - 1] = d;
  }

  return (
    <div
      className="tl"
      data-motion={reduced ? "reduce" : "full"}
      data-dragging={dragging ? "" : undefined}
      data-testid="policy-timeline"
    >
      <span className="field-label" id={`${noteId}-label`}>
        The schedule as a timeline: drag a boundary, or use the days below
      </span>
      <div className="tl-track" ref={track} role="group" aria-labelledby={`${noteId}-label`}>
        {segs.map((s) => {
          const to = s.to ?? horizon;
          const share = Math.max(0, to - s.from) / horizon;
          const cls = native(s.tier);
          const span = s.to === null ? `from day ${s.from}` : `day ${s.from} to ${s.to}`;
          return (
            <div
              key={`${s.tier}-${s.from}`}
              className="tl-seg"
              data-tier={s.tier}
              data-short={s.endsAt && shortAt.has(s.endsAt) ? "" : undefined}
              style={{ left: pct(s.from, horizon), width: pct(Math.max(0, to - s.from), horizon) }}
              title={`${tierLabel(s.tier)}${cls ? ` (${className(cls)})` : ""}, ${span}`}
            >
              {share >= 0.07 ? <span className="tl-seg-name">{tierLabel(s.tier)}</span> : null}
              {share >= 0.16 ? (
                <span className="tl-seg-sub">{cls ? className(cls) : span}</span>
              ) : null}
            </div>
          );
        })}
        {horizon > 365 ? (
          <span className="tl-year" style={{ left: pct(365, horizon) }} aria-hidden="true" />
        ) : null}
        {bounds.map((b, i) => {
          const [lo, hi] = range(i);
          const severity = checkOf(b.field);
          const notes = checks
            .filter((c) => c.field === b.field)
            .map((_, n) => `${noteId}-${b.field}-${n}`);
          return (
            <div
              key={b.key}
              className="tl-handle"
              data-tier={b.tier}
              data-check={severity}
              role="slider"
              tabIndex={0}
              aria-label={`Move to ${tierLabel(b.tier)} after, in days`}
              aria-valuemin={lo}
              aria-valuemax={hi}
              aria-valuenow={b.value}
              aria-valuetext={`Day ${b.value}: files move to ${tierLabel(b.tier)} ${b.value} days after ${clock}`}
              aria-describedby={notes.length ? notes.join(" ") : undefined}
              aria-orientation="horizontal"
              style={{ left: pct(b.value, horizon) }}
              onKeyDown={onKey(i)}
              onPointerDown={onDown(i)}
              onPointerMove={onMove(i)}
              onPointerUp={onUp}
              onPointerCancel={onUp}
            >
              {severity ? (
                <span className="tl-flag" aria-hidden="true">
                  {severity === "error" ? <ErrorIcon size={12} /> : <AlertIcon size={12} />}
                </span>
              ) : null}
              <span className="tl-knob" aria-hidden="true" />
            </div>
          );
        })}
      </div>
      <div className="tl-axis" aria-hidden="true">
        {spaced.map((d) => (
          <span
            key={d}
            className="tl-tick"
            data-edge={d === 0 ? "start" : d / horizon > 0.95 ? "end" : undefined}
            style={{ left: pct(d, horizon) }}
          >
            {d === 365 && !bounds.some((b) => b.value === 365) ? "1 yr" : `day ${d}`}
          </span>
        ))}
      </div>
      {checks.length > 0 ? (
        <ul className="tl-notes" aria-label="On the timeline">
          {FIELDS.flatMap((field) =>
            checks
              .filter((c) => c.field === field)
              .map((c, n) => (
                <li
                  key={`${field}-${c.code}-${n}`}
                  id={`${noteId}-${field}-${n}`}
                  data-severity={c.severity}
                >
                  {c.severity === "error" ? (
                    <ErrorIcon size={14} />
                  ) : c.severity === "warning" ? (
                    <AlertIcon size={14} />
                  ) : (
                    <InfoIcon size={14} />
                  )}
                  <span>{c.message}</span>
                </li>
              )),
          )}
        </ul>
      ) : null}
      <p className="tl-help">
        {smart
          ? "Smart: the provider moves each file among Hot, Cool and Cold by use. The archive day always holds, counted from the write."
          : `Days count from ${clock}. A dashed line marks one year.`}
      </p>
      <TierLegend
        tiers={smart ? ["smart", "archive"] : ["hot", "warm", "cold", "archive"]}
        label="Colour key for the timeline"
      />
    </div>
  );
}
