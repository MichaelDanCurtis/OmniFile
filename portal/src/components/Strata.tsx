import type { PolicyPreview, TimelineSegment } from "../api/types";
import { accountLabel, tierLabel } from "../lib/format";

type Timeline = PolicyPreview["timeline"];

const pct = (day: number, horizon: number) => `${Math.min(100, (day / horizon) * 100)}%`;

function segmentLabel(seg: TimelineSegment): string {
  const end = seg.to_day === null || seg.to_day === undefined ? "onwards" : `to day ${seg.to_day}`;
  const where = seg.local === "stub" ? "edge keeps a stub" : "edge keeps the file";
  const account = seg.account && seg.account !== "single" ? `, ${accountLabel(seg.account)}` : "";
  return `${tierLabel(seg.logical_tier)}${seg.native_class && seg.native_class !== tierLabel(seg.logical_tier) ? ` (${seg.native_class})` : ""} from day ${seg.from_day} ${end}${account}; ${where}${seg.note ? `. ${seg.note}` : ""}`;
}

/**
 * The signature view: a file's life as strata. The top lane is where the
 * bytes sit in the cloud (tier by tier), the thin lane what the edge keeps.
 * A dashed line marks the end of the first year, the span the cost covers.
 */
export function Strata({ timeline }: { timeline: Timeline }) {
  const horizon = Math.max(timeline.horizon_days, 1);
  const segments = timeline.segments;
  const ticks = new Set<number>([0]);
  for (const s of segments) ticks.add(s.from_day);
  ticks.add(365);
  const tickList = [...ticks].filter((d) => d <= horizon).sort((a, b) => a - b);
  // Drop ticks that would collide (closer than ~5% of the width).
  const spaced: number[] = [];
  for (const d of tickList) {
    const prev = spaced.at(-1);
    if (prev === undefined || d - prev > horizon * 0.09) spaced.push(d);
    else if (d === 365) spaced[spaced.length - 1] = d;
  }

  const summary = segments.map(segmentLabel).join(". ");
  const clock = timeline.clock === "written" ? "the day a file is written" : "a file's last read";

  return (
    <figure className="strata" aria-label="Tier timeline">
      <div className="strata-lane">
        <span className="strata-lane-label">Cloud</span>
        <div
          className="strata-track"
          role="img"
          aria-label={`Days count from ${clock}. ${summary}.`}
        >
          {segments.map((seg) => {
            const to = seg.to_day ?? horizon;
            const share = (to - seg.from_day) / horizon;
            return (
              <div
                key={`${seg.logical_tier}-${seg.from_day}`}
                className="strata-seg"
                data-tier={seg.logical_tier}
                style={{ left: pct(seg.from_day, horizon), width: pct(to - seg.from_day, horizon) }}
                title={segmentLabel(seg)}
              >
                {share >= 0.08 ? (
                  <span className="strata-seg-name">{tierLabel(seg.logical_tier)}</span>
                ) : null}
                {share < 0.16 ? null : (
                  <span className="strata-seg-sub">
                    {seg.native_class && seg.native_class !== tierLabel(seg.logical_tier)
                      ? seg.native_class
                      : seg.account && seg.account !== "single"
                        ? accountLabel(seg.account)
                        : `day ${seg.from_day}`}
                  </span>
                )}
              </div>
            );
          })}
          {horizon > 365 ? (
            <span className="strata-year" style={{ left: pct(365, horizon) }} />
          ) : null}
        </div>
      </div>
      <div className="strata-lane">
        <span className="strata-lane-label">Edge</span>
        <div className="strata-track strata-track-thin" aria-hidden="true">
          {segments.map((seg) => (
            <div
              key={`local-${seg.from_day}`}
              className="strata-local"
              data-local={seg.local}
              style={{
                left: pct(seg.from_day, horizon),
                width: pct((seg.to_day ?? horizon) - seg.from_day, horizon),
              }}
            />
          ))}
        </div>
      </div>
      <div className="strata-axis" aria-hidden="true">
        {spaced.map((day) => (
          <span
            key={day}
            className="strata-tick"
            data-edge={day === 0 ? "start" : undefined}
            style={{ left: pct(day, horizon) }}
          >
            {day === 365 ? "1 yr" : `day ${day}`}
          </span>
        ))}
      </div>
      <figcaption className="strata-legend">
        <span>
          <span className="legend-swatch" data-swatch="resident" />
          File on the edge
        </span>
        <span>
          <span className="legend-swatch" data-swatch="stub" />
          Stub on the edge (first MB kept)
        </span>
        <span>Days count from {clock}.</span>
      </figcaption>
    </figure>
  );
}
