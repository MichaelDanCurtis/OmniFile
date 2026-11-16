import { useId, useMemo, useState } from "react";
import { Link } from "react-router";
import { useShares } from "../../../api/queries";
import { Pills } from "../../../components/PageHead";
import { ErrorCallout } from "../../../components/Feedback";
import { Tip } from "../../../components/Tip";
import { DotField } from "../../../theme/DotField";
import {
  RECALL_STATE_LABEL,
  formatBytes,
  formatDateTime,
  formatRelative,
} from "../../../lib/format";
import { useLiveFeed, type LiveEdge, type LiveEvent } from "./useLiveFeed";
import "./live.css";

export const KIND_LABEL: Record<string, string> = {
  upload: "Upload",
  recall: "Recall",
  tier: "Tier move",
  heartbeat: "Heartbeat",
  migration: "Migration",
  annex: "Annex",
};
const KINDS = Object.keys(KIND_LABEL);

const EDGE_STATUS: Record<string, { label: string; tone: string }> = {
  ok: { label: "Online", tone: "ok" },
  attention: { label: "Needs attention", tone: "warn" },
  silent: { label: "Not reporting", tone: "signal" },
  pending: { label: "Not enrolled", tone: "neutral" },
};

const MIGRATION_STATE: Record<string, string> = {
  started: "started",
  verified: "verified",
  cut_over: "cut over",
  failed: "failed",
  cancelled: "cancelled",
  handed_off: "handed off",
};

const ANNEX_KIND: Record<string, string> = {
  arrived: "a file arrived",
  changed: "a file changed",
  left: "a file left the NAS",
  renamed: "a file was renamed",
  copied: "copied",
  verified: "copy verified",
  copy_failed: "copy failed",
  skipped: "skipped",
  drift: "drift found",
  scan: "scanned",
  mount: "mount checked",
  purged: "purged",
  would_remove: "would remove",
};

const RECALL_LABELS: Record<string, string> = RECALL_STATE_LABEL;

const pick = (labels: Record<string, string>, v: string | null | undefined) =>
  v ? (labels[v] ?? v) : "";

/** What the file is called, or that it's one the viewer may not read (no name given). */
function fileWord(e: LiveEvent): string {
  if (e.name) return e.name;
  if (e.hidden) return "a file you can't read";
  return "a file";
}

/** One plain sentence per event. */
export function describe(e: LiveEvent): string {
  const d = e.detail as Record<string, string | number | undefined>;
  switch (e.kind) {
    case "upload":
      return `Uploaded ${fileWord(e)}`;
    case "recall":
      return `Recall ${pick(RECALL_LABELS, e.state).toLowerCase()}: ${fileWord(e)}`;
    case "tier": {
      const move = d.from && d.to ? ` from ${d.from} to ${d.to}` : d.to ? ` to ${d.to}` : "";
      if (e.state === "late") return `Archived after its date (${d.archive_at}): ${fileWord(e)}`;
      if (e.state === "waiting") return `Move${move} waiting: ${fileWord(e)}`;
      return e.state === "refused"
        ? `Tier move refused${move}: ${fileWord(e)}`
        : `Moved ${fileWord(e)}${move}`;
    }
    case "heartbeat":
      return `${e.edge_name ?? "An edge"} reported in${d.software_version ? ` (${d.software_version})` : ""}`;
    case "migration":
      return `Migration ${d.migration_id ?? ""} ${pick(MIGRATION_STATE, e.state)}`.trim();
    case "annex":
      return `Annex ${d.annex_name ?? ""}: ${pick(ANNEX_KIND, e.state)}${e.name || e.hidden ? `, ${fileWord(e)}` : ""}`;
    default:
      return `${e.kind}${e.state ? ` ${e.state}` : ""}`;
  }
}

/** A tiny line of the last values, scaled to its own maximum. */
export function Sparkline({ values, label }: { values: number[]; label: string }) {
  const w = 120;
  const h = 28;
  const pts = values.length > 1 ? values : [values[0] ?? 0, values[0] ?? 0];
  const max = Math.max(1, ...pts);
  const step = w / (pts.length - 1);
  const d = pts
    .map((v, i) => `${(i * step).toFixed(1)},${(h - 2 - (v / max) * (h - 4)).toFixed(1)}`)
    .join(" ");
  return (
    <svg
      className="spark"
      viewBox={`0 0 ${w} ${h}`}
      preserveAspectRatio="none"
      role="img"
      aria-label={label}
    >
      <polyline points={d} />
    </svg>
  );
}

function Stat({
  label,
  value,
  unit,
  values,
  trend,
}: {
  label: string;
  value: string;
  unit?: string;
  values: number[];
  trend: string;
}) {
  return (
    <div className="live-stat">
      <span className="live-stat-label">{label}</span>
      <span className="live-stat-value">
        <span className="live-stat-num">{value}</span>
        {unit ? <span className="live-stat-unit">{unit}</span> : null}
      </span>
      <Sparkline values={values} label={trend} />
    </div>
  );
}

function EdgeStrip({
  edges,
  selected,
  onSelect,
}: {
  edges: LiveEdge[];
  selected: string;
  onSelect: (id: string) => void;
}) {
  if (edges.length === 0) {
    return (
      <p className="muted">
        No edges yet. <Link to="/edges/new">Deploy an edge</Link> to see it here.
      </p>
    );
  }
  return (
    <ul className="edge-strip" aria-label="Edges">
      {edges.map((e) => {
        const s = EDGE_STATUS[e.status] ?? { label: e.status, tone: "neutral" };
        const on = selected === e.edge_id;
        return (
          <li key={e.edge_id}>
            <button
              type="button"
              className="edge-chip"
              data-tone={s.tone}
              aria-pressed={on}
              aria-label={`${e.name}: ${s.label}. Show only this edge's events`}
              onClick={() => onSelect(on ? "" : e.edge_id)}
            >
              <span className="edge-chip-head">
                <span className="edge-dot" aria-hidden="true" />
                <span className="edge-chip-name">{e.name}</span>
              </span>
              <span className="edge-chip-status">{s.label}</span>
              <span className="edge-chip-nums">
                <span>{e.uploads_15m} up</span>
                <span>{e.recalls_open} recalling</span>
                {e.outbox_depth !== null ? <span>{e.outbox_depth} queued</span> : null}
              </span>
              <span className="edge-chip-seen">
                {e.last_heartbeat_at
                  ? `heard ${formatRelative(e.last_heartbeat_at)}`
                  : "never heard"}
              </span>
            </button>
          </li>
        );
      })}
    </ul>
  );
}

function FeedRow({ e, fresh }: { e: LiveEvent; fresh: boolean }) {
  const d = e.detail as Record<string, string | undefined>;
  const link =
    e.kind === "migration" && d.migration_id
      ? `/migrations/${d.migration_id}`
      : e.kind === "recall" && d.recall_id
        ? `/recalls/${d.recall_id}`
        : e.kind === "annex" && d.annex_id
          ? `/annexes/${d.annex_id}`
          : null;
  return (
    <li className="live-row" data-kind={e.kind} data-fresh={fresh || undefined}>
      <time className="live-at" dateTime={e.at}>
        <Tip label="When" tip={formatDateTime(e.at)}>
          {new Date(e.at).toLocaleTimeString(undefined, {
            hour: "2-digit",
            minute: "2-digit",
            second: "2-digit",
            hour12: false,
          })}
        </Tip>
      </time>
      <span className="live-kind" data-kind={e.kind}>
        {KIND_LABEL[e.kind] ?? e.kind}
      </span>
      <span className="live-what">
        <span className="live-sentence">
          {link ? <Link to={link}>{describe(e)}</Link> : describe(e)}
          {e.bytes ? <span className="muted num"> · {formatBytes(e.bytes)}</span> : null}
        </span>
        {e.path && e.path !== e.name ? <span className="live-path mono">{e.path}</span> : null}
      </span>
      <span className="live-where">
        {e.share_name ? <span>{e.share_name}</span> : null}
        {e.edge_name ? <span className="muted">{e.edge_name}</span> : null}
      </span>
      <span className="live-who">{e.person?.display_name ?? ""}</span>
    </li>
  );
}

/**
 * The live part of Activity (WP18): counters with sparklines, the edges, and a
 * feed of what is happening now. Filters apply to what has arrived; Pause stops
 * the requests and keeps the list still.
 */
export function LivePanel() {
  const [paused, setPaused] = useState(false);
  const feed = useLiveFeed(paused);
  const shares = useShares();
  const [kinds, setKinds] = useState<Set<string>>(new Set(KINDS));
  const [edge, setEdge] = useState("");
  const [share, setShare] = useState("");
  const [person, setPerson] = useState("");
  const ids = { edge: useId(), share: useId(), person: useId(), live: useId() };
  const latest = feed.latest;

  const people = useMemo(() => {
    const m = new Map<string, string>();
    for (const e of feed.events) {
      if (e.person)
        m.set(e.person.identity_id, e.person.display_name ?? `#${e.person.identity_id}`);
    }
    return [...m].sort((a, b) => a[1].localeCompare(b[1]));
  }, [feed.events]);

  const shown = feed.events.filter(
    (e) =>
      kinds.has(e.kind) &&
      (!edge || e.edge_id === edge) &&
      (!share || e.share_id === share) &&
      (!person || e.person?.identity_id === person),
  );
  const toggleKind = (k: string) =>
    setKinds((cur) => {
      const next = new Set(cur);
      if (next.has(k)) next.delete(k);
      else next.add(k);
      return next;
    });
  const c = latest?.counters;
  const status =
    feed.status === "paused"
      ? "Paused"
      : feed.status === "retrying"
        ? "Reconnecting…"
        : feed.status === "connecting"
          ? "Connecting…"
          : feed.status === "capped"
            ? "Live (busy: polling every 10 s)"
            : "Live";

  return (
    <section className="card live" aria-labelledby="live-h">
      <div className="live-head" data-dot-host>
        <DotField
          className="live-dots"
          pulseKey={feed.events[0]?.id}
          pulseAt={[0.08, 0.5]}
          density={0.28}
          interactive={false}
        />
        <div className="live-title">
          <h2 id="live-h">Now</h2>
          <span className="live-status" data-status={feed.status}>
            <span className="live-beat" aria-hidden="true" />
            {status}
          </span>
        </div>
        <button
          type="button"
          className="btn"
          aria-pressed={paused}
          onClick={() => setPaused(!paused)}
        >
          {paused ? "Resume live feed" : "Pause live feed"}
        </button>
      </div>
      {feed.status === "retrying" ? (
        <div className="live-body">
          <ErrorCallout error={feed.error} title="The live feed lost the service; trying again" />
        </div>
      ) : null}
      <div className="live-stats">
        <Stat
          label="Uploads per minute"
          value={String(c?.uploads_per_min ?? 0)}
          unit={`${c?.uploads_15m ?? 0} in 15 min`}
          values={latest?.series.uploads ?? []}
          trend="Uploads per minute, the last 30 minutes"
        />
        <Stat
          label="Bytes moved, 15 min"
          value={formatBytes(c?.bytes_moved_15m ?? 0).split(" ")[0] ?? "0"}
          unit={formatBytes(c?.bytes_moved_15m ?? 0).split(" ")[1]}
          values={latest?.series.bytes_moved ?? []}
          trend="Bytes uploaded and recalled per minute, the last 30 minutes"
        />
        <Stat
          label="Recalls in flight"
          value={String(c?.recalls_in_flight ?? 0)}
          values={feed.inflight}
          trend="Recalls in flight, since this page opened"
        />
        <Stat
          label="Edges online"
          value={String(c?.edges_online ?? 0)}
          unit={`/ ${c?.edges_total ?? 0}`}
          values={feed.online}
          trend="Edges online, since this page opened"
        />
      </div>
      <div className="live-body">
        <EdgeStrip edges={latest?.edges ?? []} selected={edge} onSelect={setEdge} />
      </div>
      <div className="live-body live-filters">
        <Pills label="Show">
          {KINDS.map((k) => (
            <button
              key={k}
              type="button"
              className="pill pill-toggle"
              data-kind={k}
              aria-pressed={kinds.has(k)}
              onClick={() => toggleKind(k)}
            >
              {KIND_LABEL[k]}
            </button>
          ))}
        </Pills>
        <Pills label="Where">
          <span className="pill">
            <label className="pill-key" htmlFor={ids.edge}>
              Edge
            </label>
            <select id={ids.edge} value={edge} onChange={(e) => setEdge(e.target.value)}>
              <option value="">All</option>
              {(latest?.edges ?? []).map((e) => (
                <option key={e.edge_id} value={e.edge_id}>
                  {e.name}
                </option>
              ))}
            </select>
          </span>
          <span className="pill">
            <label className="pill-key" htmlFor={ids.share}>
              Share
            </label>
            <select id={ids.share} value={share} onChange={(e) => setShare(e.target.value)}>
              <option value="">All</option>
              {(shares.data?.items ?? []).map((s) => (
                <option key={s.share_id} value={s.share_id}>
                  {s.name}
                </option>
              ))}
            </select>
          </span>
          <span className="pill">
            <label className="pill-key" htmlFor={ids.person}>
              Person
            </label>
            <select id={ids.person} value={person} onChange={(e) => setPerson(e.target.value)}>
              <option value="">Anyone</option>
              {people.map(([id, name]) => (
                <option key={id} value={id}>
                  {name}
                </option>
              ))}
            </select>
          </span>
        </Pills>
      </div>
      <p id={ids.live} className="sr-only" aria-live="polite">
        {feed.arrived > 0 && !paused
          ? `${feed.arrived} new ${feed.arrived === 1 ? "event" : "events"}`
          : ""}
      </p>
      {shown.length === 0 ? (
        <p className="live-empty muted">
          {feed.status === "connecting"
            ? "Listening…"
            : feed.events.length === 0
              ? "Nothing has happened in the last 15 minutes."
              : "Nothing matches these filters."}
        </p>
      ) : (
        <ol className="live-feed" aria-label="Live events">
          {shown.map((e) => (
            <FeedRow key={e.id} e={e} fresh={feed.fresh.has(e.id)} />
          ))}
        </ol>
      )}
    </section>
  );
}
