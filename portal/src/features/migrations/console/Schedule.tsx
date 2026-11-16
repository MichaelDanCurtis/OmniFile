import { useRef, useState, type KeyboardEvent, type PointerEvent } from "react";
import type { MigrationPlan, MigrationWave } from "./consoleApi";
import { formatUtc, waveStateLabel } from "./consoleLabels";
import "./console.css";

const HOUR = 3_600_000;
const DAY = 24 * HOUR;
const ms = (iso: string | null | undefined) => (iso ? new Date(iso).getTime() : null);

/** Where a wave may start: not before the wave it waits for, not after one that waits for it. */
export function rangeOf(plan: MigrationPlan, w: MigrationWave): [number | null, number | null] {
  const dep = w.after ? plan.waves.find((x) => x.seq === w.after) : undefined;
  const lo = dep ? ms(dep.start_at) : null;
  const later = plan.waves.filter((x) => x.after === w.seq).map((x) => ms(x.start_at) ?? Infinity);
  const hi = later.length ? Math.min(...later) : null;
  return [lo, hi];
}

export function movable(plan: MigrationPlan, w: MigrationWave): boolean {
  return (
    (plan.state === "approved" || plan.state === "proposed") &&
    (w.state === "planned" || w.state === "waiting" || w.state === "blocked")
  );
}

/**
 * The plan's waves on a timeline: each wave's planned start (a handle you can
 * drag, or move with the arrow keys: an hour, Shift a day), its actual run
 * from start to cutover, the verified and cutover marks, its blackouts and
 * now. A move is held until Save, and checked against the wave's
 * dependencies as it goes; the server checks windows and blackouts again.
 */
export function Schedule({
  plan,
  now: nowProp,
  onSave,
  saving,
}: {
  plan: MigrationPlan;
  /** Now, for the now line and the scale (tests pass one). */
  now?: number;
  onSave: (seq: number, startAt: string) => void;
  saving: boolean;
}) {
  const [mounted] = useState(() => Date.now());
  const now = nowProp ?? mounted;
  const [pending, setPending] = useState<Record<number, number>>({});
  const [dragging, setDragging] = useState<number | null>(null);
  const track = useRef<HTMLDivElement>(null);
  const starts = plan.waves.map((w) => pending[w.seq] ?? ms(w.start_at) ?? now);
  const actual = plan.waves.flatMap((w) =>
    [ms(w.started_at), ms(w.verified_at), ms(w.cut_over_at)].filter((x): x is number => x !== null),
  );
  const [frozen, setFrozen] = useState<[number, number] | null>(null);
  const lo0 = Math.min(now, ...starts, ...actual) - 6 * HOUR;
  const hi0 = Math.max(now, ...starts, ...actual) + 2 * DAY;
  const [t0, t1] = frozen ?? [lo0, Math.max(hi0, lo0 + 3 * DAY)];
  const pct = (t: number) => `${Math.max(0, Math.min(100, ((t - t0) / (t1 - t0)) * 100))}%`;
  const width = (a: number, b: number) =>
    `${Math.max(0.4, Math.min(100, ((Math.min(b, t1) - Math.max(a, t0)) / (t1 - t0)) * 100))}%`;

  const clamp = (w: MigrationWave, t: number) => {
    const [lo, hi] = rangeOf(plan, w);
    let v = Math.round(t / (15 * 60_000)) * 15 * 60_000; // quarter hours
    if (lo !== null) v = Math.max(lo, v);
    if (hi !== null) v = Math.min(hi, v);
    return v;
  };
  const move = (w: MigrationWave, t: number) => setPending((p) => ({ ...p, [w.seq]: clamp(w, t) }));
  const at = (clientX: number) => {
    const r = track.current?.getBoundingClientRect();
    if (!r || r.width === 0) return null;
    return t0 + ((clientX - r.left) / r.width) * (t1 - t0);
  };
  const onKey = (w: MigrationWave, value: number) => (e: KeyboardEvent<HTMLDivElement>) => {
    const step = e.shiftKey ? DAY : HOUR;
    const [lo, hi] = rangeOf(plan, w);
    const to: Record<string, number | null> = {
      ArrowLeft: value - step,
      ArrowDown: value - step,
      ArrowRight: value + step,
      ArrowUp: value + step,
      PageDown: value - DAY,
      PageUp: value + DAY,
      Home: lo,
      End: hi,
    };
    const t = to[e.key];
    if (t === undefined || t === null) return;
    e.preventDefault();
    move(w, t);
  };
  const ticks: number[] = [];
  for (let d = Math.ceil(t0 / DAY) * DAY; d <= t1; d += DAY) ticks.push(d);
  const every = Math.max(1, Math.ceil(ticks.length / 10));

  return (
    <div className="sched" data-dragging={dragging !== null ? "" : undefined}>
      <p className="field-label" id={`sched-${plan.plan_id}`}>
        The schedule: drag a wave&apos;s start, or focus it and use the arrow keys (an hour; Shift,
        a day). Times are UTC.
      </p>
      <div className="sched-grid" role="group" aria-labelledby={`sched-${plan.plan_id}`}>
        <div className="sched-axis" aria-hidden="true">
          {ticks.map((d, i) =>
            i % every === 0 ? (
              <span key={d} className="sched-tick" style={{ left: pct(d) }}>
                {new Date(d).toISOString().slice(5, 10)}
              </span>
            ) : null,
          )}
        </div>
        {plan.waves.map((w, i) => {
          const value = starts[i] ?? now;
          const can = movable(plan, w);
          const [lo, hi] = rangeOf(plan, w);
          const start = ms(w.started_at);
          const end = ms(w.cut_over_at) ?? (start !== null ? now : null);
          const changed = pending[w.seq] !== undefined && pending[w.seq] !== ms(w.start_at);
          return (
            <div key={w.seq} className="sched-lane" data-state={w.state}>
              <div className="sched-label">
                <strong>
                  {w.seq}. {w.name}
                </strong>
                <span className="muted">
                  {waveStateLabel(w.state)}
                  {w.after ? ` · after wave ${w.after}` : ""}
                </span>
              </div>
              <div className="sched-track" ref={i === 0 ? track : undefined}>
                <span className="sched-now" style={{ left: pct(now) }} aria-hidden="true" />
                {w.blackouts.map((b) => {
                  const a = ms(b.from) ?? 0;
                  const z = ms(b.to) ?? 0;
                  return z < t0 || a > t1 ? null : (
                    <span
                      key={b.from}
                      className="sched-blackout"
                      style={{ left: pct(a), width: width(a, z) }}
                      title={`Blackout ${formatUtc(b.from)} to ${formatUtc(b.to)}`}
                    />
                  );
                })}
                {start !== null && end !== null ? (
                  <span
                    className="sched-actual"
                    data-state={w.state}
                    style={{ left: pct(start), width: width(start, end) }}
                    title={`Ran from ${formatUtc(w.started_at)}${w.cut_over_at ? ` to cutover ${formatUtc(w.cut_over_at)}` : ""}`}
                  />
                ) : null}
                {w.verified_at ? (
                  <span
                    className="sched-mark"
                    data-mark="verified"
                    style={{ left: pct(ms(w.verified_at) ?? 0) }}
                    title={`Verified ${formatUtc(w.verified_at)}`}
                  />
                ) : null}
                {w.cut_over_at ? (
                  <span
                    className="sched-mark"
                    data-mark="cutover"
                    style={{ left: pct(ms(w.cut_over_at) ?? 0) }}
                    title={`Cut over ${formatUtc(w.cut_over_at)}`}
                  />
                ) : null}
                <div
                  className="sched-handle"
                  role="slider"
                  tabIndex={can ? 0 : -1}
                  aria-disabled={!can}
                  aria-label={`Planned start of wave ${w.seq}, ${w.name}`}
                  aria-valuemin={lo ?? undefined}
                  aria-valuemax={hi ?? undefined}
                  aria-valuenow={value}
                  aria-valuetext={`${formatUtc(new Date(value).toISOString())}${changed ? ", not saved" : ""}`}
                  data-changed={changed ? "" : undefined}
                  style={{ left: pct(value) }}
                  onKeyDown={can ? onKey(w, value) : undefined}
                  onPointerDown={
                    can
                      ? (e: PointerEvent<HTMLDivElement>) => {
                          e.preventDefault();
                          try {
                            e.currentTarget.setPointerCapture(e.pointerId);
                          } catch {
                            // no capture: moves still arrive on the handle
                          }
                          e.currentTarget.focus();
                          setDragging(w.seq);
                          setFrozen([t0, t1]);
                        }
                      : undefined
                  }
                  onPointerMove={(e) => {
                    if (dragging !== w.seq) return;
                    const t = at(e.clientX);
                    if (t !== null) move(w, t);
                  }}
                  onPointerUp={() => {
                    setDragging(null);
                    setFrozen(null);
                  }}
                  onPointerCancel={() => {
                    setDragging(null);
                    setFrozen(null);
                  }}
                />
              </div>
              {changed ? (
                <div className="row sched-save">
                  <span>
                    New start {formatUtc(new Date(value).toISOString())}
                    {lo !== null && value === lo ? ` (as early as wave ${String(w.after)})` : ""}
                  </span>
                  <button
                    type="button"
                    className="btn btn-sm btn-primary"
                    disabled={saving}
                    onClick={() => {
                      onSave(w.seq, new Date(value).toISOString());
                      setPending((p) => {
                        const { [w.seq]: _gone, ...rest } = p;
                        return rest;
                      });
                    }}
                  >
                    Save
                  </button>
                  <button
                    type="button"
                    className="btn btn-sm btn-ghost"
                    onClick={() =>
                      setPending((p) => {
                        const { [w.seq]: _gone, ...rest } = p;
                        return rest;
                      })
                    }
                  >
                    Undo
                  </button>
                </div>
              ) : null}
            </div>
          );
        })}
      </div>
      <ul className="sched-legend" aria-label="Legend">
        <li>
          <span className="sched-key" data-key="planned" /> Planned start
        </li>
        <li>
          <span className="sched-key" data-key="actual" /> Ran (start to cutover, or now)
        </li>
        <li>
          <span className="sched-key" data-key="verified" /> Verified
        </li>
        <li>
          <span className="sched-key" data-key="cutover" /> Cut over
        </li>
        <li>
          <span className="sched-key" data-key="blackout" /> Blackout
        </li>
      </ul>
    </div>
  );
}
