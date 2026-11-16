import { useId, useState, type ReactNode } from "react";
import { Link, useParams } from "react-router";
import { usePageTitle } from "../../app/usePageTitle";
import { ErrorCallout, Loading } from "../../components/Feedback";
import { Modal } from "../../components/Modal";
import { HeroMetric, PageHead, Pills } from "../../components/PageHead";
import { RulesSection } from "./RulesSection";
import { FilerAccess, FilerAcls } from "./FilerAccess";
import { GetBack, canGetBack } from "./GetBack";
import { useDebounced } from "../../lib/useDebounced";
import {
  formatBytes,
  formatDate,
  formatDateTime,
  formatRelative,
  tierLabel,
} from "../../lib/format";
import {
  COPY_LABEL,
  DRIFT_LABEL,
  NAS_LABEL,
  STATE_LABEL,
  type AnnexFile,
  type AnnexProblems,
  type AnnexTimeline,
  label,
  useAnnex,
  useAnnexFiles,
  useAnnexProblems,
  useAnnexTimeline,
  useCopyRefusedAction,
  usePatchAnnex,
  usePurgeAnnexFile,
} from "./api";

const DAYS = [7, 30, 90] as const;

/**
 * Arrivals and departures per day: arrivals in grey, departures from the NAS
 * in the signal colour (the series that matters: those copies are now the
 * last ones), copies as ink ticks, and the daily average of arrivals as a
 * dashed line with its value in a pill.
 */
function TimelineChart({ t }: { t: AnnexTimeline }) {
  const width = 600;
  const height = 140;
  const n = Math.max(1, t.days.length);
  const max = Math.max(1, ...t.days.map((d) => Math.max(d.arrived + d.changed, d.left, d.copied)));
  const slot = width / n;
  const bar = Math.max(1, slot / 2 - 1);
  const arrived = t.days.reduce((s, d) => s + d.arrived, 0);
  const left = t.days.reduce((s, d) => s + d.left, 0);
  const copied = t.days.reduce((s, d) => s + d.copied, 0);
  const avg = t.days.length ? t.days.reduce((s, d) => s + d.arrived + d.changed, 0) / n : 0;
  const yAvg = height - (avg / max) * (height - 4);
  const first = t.days[0];
  const last = t.days[t.days.length - 1];
  return (
    <div className="chart">
      <div className="chart-plot">
        <svg
          className="chart-svg annex-timeline"
          viewBox={`0 0 ${width} ${height}`}
          role="img"
          aria-label={`Over ${t.days.length} days: ${arrived} arrived, ${left} left the NAS, ${copied} copied`}
          preserveAspectRatio="none"
        >
          <g className="chart-grid" aria-hidden="true">
            {[0.25, 0.5, 0.75, 1].map((f) => (
              <line
                key={f}
                x1={0}
                x2={width}
                y1={height - f * (height - 4)}
                y2={height - f * (height - 4)}
              />
            ))}
          </g>
          {t.days.map((d, i) => {
            const hIn = Math.round(((d.arrived + d.changed) / max) * (height - 4));
            const hOut = Math.round((d.left / max) * (height - 4));
            const hCopy = Math.round((d.copied / max) * (height - 4));
            return (
              <g key={d.date}>
                <title>{`${formatDate(d.date)}: ${d.arrived} arrived, ${d.changed} changed, ${d.left} left, ${d.copied} copied`}</title>
                <rect className="in" x={i * slot + 1} y={height - hIn} width={bar} height={hIn} />
                <rect
                  className="out"
                  x={i * slot + 1 + bar}
                  y={height - hOut}
                  width={bar}
                  height={hOut}
                />
                {hCopy > 0 ? (
                  <line
                    className="copy"
                    x1={i * slot + 1}
                    x2={i * slot + 1 + 2 * bar}
                    y1={height - hCopy}
                    y2={height - hCopy}
                  />
                ) : null}
              </g>
            );
          })}
          {avg > 0 ? <line className="avg" x1={0} x2={width} y1={yAvg} y2={yAvg} /> : null}
        </svg>
        {avg > 0 ? (
          <span
            className="chart-note"
            style={{ top: `${(yAvg / height) * 100}%` }}
            aria-hidden="true"
          >
            avg {avg >= 10 ? Math.round(avg) : avg.toFixed(1)} in a day
          </span>
        ) : null}
      </div>
      {first && last ? (
        <div className="chart-axis" aria-hidden="true">
          <span>{formatDate(first.date)}</span>
          <span>{formatDate(last.date)}</span>
        </div>
      ) : null}
    </div>
  );
}

/** How soon a problem needs a person: failed copies first, then drift, then the rest. */
function eventTag(kind: string): { tone: "signal" | "neutral"; word: string; rank: number } {
  if (kind === "copy_failed") return { tone: "signal", word: "Failed", rank: 0 };
  if (kind === "skipped") return { tone: "neutral", word: "Note", rank: 2 };
  return { tone: "neutral", word: "Event", rank: 3 };
}

/** The tier track with the NAS as its first stop. */
function Track({ f }: { f: AnnexFile }) {
  const nas = f.nas_state === "present";
  return (
    <span className="track">
      <span className={nas ? "badge badge-ok" : "badge badge-outline"}>
        {nas ? "NAS" : "Not on NAS"}
      </span>
      {" → "}
      {f.tier ? (
        <span className="badge badge-info">{tierLabel(f.tier)}</span>
      ) : (
        <span className="muted">Not in Azure yet</span>
      )}
    </span>
  );
}

type RefusedCopy = AnnexProblems["copies_refused"] extends (infer T)[] | undefined ? T : never;

/** A copy Azure's read-back keeps refusing: what was done about it, and, for an
 * ArchiveAdmin, stop or resume copying it again, or acknowledge the alert. */
function RefusedCopy({ r, annexId, canAct }: { r: RefusedCopy; annexId: string; canAct: boolean }) {
  const act = useCopyRefusedAction(annexId);
  const times = r.copy_refusals === 1 ? "once" : `${r.copy_refusals} times in a row`;
  return (
    <>
      <strong>
        {r.copy_stopped ? "Copying stopped: Azure refused the copy" : "Azure refused the copy"}
      </strong>
      <span className="mono">{r.path}</span>
      <span className="finding-msg">
        Azure&apos;s read-back refused it {times}
        {r.copy_refused_at ? ` (last ${formatRelative(r.copy_refused_at)})` : ""}, so nothing leaves
        the NAS for it.{" "}
        {r.copy_stopped
          ? `${r.copy_stopped.by} stopped copying it again ${formatRelative(r.copy_stopped.at)}: it isn't uploaded again until a copy verifies or copying is resumed.`
          : r.at_bound
            ? "The service no longer asks for it; the edge's reconciliation still copies it again."
            : "The edge is asked to copy it again."}
        {r.copy_refused_ack
          ? ` ${r.copy_refused_ack.by} acknowledged the alert ${formatRelative(r.copy_refused_ack.at)}.`
          : ""}
      </span>
      {canAct ? (
        <span className="finding-actions">
          <button
            type="button"
            className="btn btn-sm"
            disabled={act.isPending}
            aria-label={`${r.copy_stopped ? "Resume re-copying" : "Stop re-copying"} ${r.path}`}
            onClick={() =>
              act.mutate({
                fileId: r.file_id,
                action: r.copy_stopped ? "copy-resume" : "copy-stop",
              })
            }
          >
            {r.copy_stopped ? "Resume re-copying" : "Stop re-copying"}
          </button>
          {r.alerting ? (
            <button
              type="button"
              className="btn btn-sm"
              disabled={act.isPending}
              aria-label={`Acknowledge the alert for ${r.path}`}
              onClick={() => act.mutate({ fileId: r.file_id, action: "copy-refused-ack" })}
            >
              Acknowledge the alert
            </button>
          ) : null}
        </span>
      ) : null}
      {act.isError ? <ErrorCallout error={act.error} /> : null}
    </>
  );
}

/** The annex's problems as a findings card: failed copies first, then drift, then the rest. */
function ProblemsCard({
  problems,
  annexId,
  canAct,
}: {
  problems: AnnexProblems | undefined;
  annexId: string;
  canAct: boolean;
}) {
  type Row = {
    key: string;
    rank: number;
    tone: "signal" | "warn" | "neutral";
    word: string;
    body: ReactNode;
  };
  const rows: Row[] = [];
  // Decision 63: files are hidden until every folder above them has been read.
  const rs = problems?.read_set;
  if (rs && !rs.filer_answered) {
    rows.push({
      key: "filer-access",
      rank: 0,
      tone: "signal",
      word: "Hidden",
      body: (
        <>
          <strong>The filer&apos;s access question isn&apos;t answered</strong>
          <span className="finding-msg">
            Nobody sees this annex&apos;s files until an ArchiveAdmin says whether its share or
            export lets in fewer people than the files&apos; own permissions do.
          </span>
        </>
      ),
    });
  }
  if (rs && (!rs.ready || rs.unreadable > 0)) {
    rows.push({
      key: "read-set",
      rank: 0,
      tone: "signal",
      word: "Hidden",
      body: !rs.ready ? (
        <>
          <strong>Files are hidden until their folders are read</strong>
          <span className="finding-msg">
            Nobody sees this annex&apos;s files until the edge has read the permissions of every
            folder above them, up to the export&apos;s root ({rs.folders} folders read so far). That
            happens at its next scan.
          </span>
        </>
      ) : (
        <>
          <strong>Folders whose permissions can&apos;t be read</strong>
          <span className="finding-msg">
            {rs.unreadable} {rs.unreadable === 1 ? "folder" : "folders"}: the files below them are
            hidden from everyone until the edge can read them.
          </span>
        </>
      ),
    });
  }
  // Refused copies, however old: a stopped one stays until it verifies or is resumed.
  for (const r of problems?.copies_refused ?? []) {
    const loud = r.copy_stopped !== null || r.alerting;
    rows.push({
      key: `r${r.file_id}`,
      rank: loud ? 0 : 1,
      tone: loud ? "signal" : "warn",
      word: r.copy_stopped ? "Stopped" : "Refused",
      body: <RefusedCopy r={r} annexId={annexId} canAct={canAct} />,
    });
  }
  if (problems) {
    if (problems.drift.length === 0) {
      for (const [kind, n] of Object.entries(problems.drift_counts)) {
        rows.push({
          key: `c${kind}`,
          rank: 1,
          tone: "warn",
          word: "Check",
          body: (
            <>
              <strong>{label(DRIFT_LABEL, kind)}</strong>
              <span className="finding-msg">
                {n} found by reconciliation (an ArchiveAdmin sees which)
              </span>
            </>
          ),
        });
      }
    }
    for (const d of problems.drift) {
      rows.push({
        key: `d${d.drift_id}`,
        rank: d.resolved ? 3 : 1,
        tone: d.resolved ? "neutral" : "warn",
        word: d.resolved ? "Put right" : "Check",
        body: (
          <>
            <strong>{label(DRIFT_LABEL, d.kind)}</strong>
            <span className="mono">{d.path}</span>
            <span className="finding-msg">
              found by reconciliation {formatRelative(d.detected_at)}
              {d.resolved ? ", put right" : ""}
            </span>
          </>
        ),
      });
    }
    for (const e of problems.events) {
      const details = (e.details ?? {}) as { reason?: string; detail?: string };
      const tag = e.resolved
        ? { rank: 3, tone: "neutral" as const, word: "Put right" }
        : eventTag(e.kind);
      rows.push({
        key: `e${e.event_id}`,
        rank: tag.rank,
        tone: tag.tone,
        word: tag.word,
        body: (
          <>
            <strong>
              {e.kind === "copy_failed" ? "Copy failed" : e.kind === "skipped" ? "Skipped" : e.kind}
            </strong>
            {e.path ? <span className="mono">{e.path}</span> : null}
            <span className="finding-msg">
              {details.reason ?? ""}
              {details.detail ? `: ${details.detail}` : ""} ({formatRelative(e.at)})
            </span>
          </>
        ),
      });
    }
  }
  const ranked = rows.map((r, i) => ({ r, i })).sort((x, y) => x.r.rank - y.r.rank || x.i - y.i);
  return (
    <section className="card findings" aria-labelledby="annex-problems">
      <div className="findings-head">
        <h2 id="annex-problems">Problems</h2>
        {problems && rows.length > 0 ? (
          <span className="findings-count">({rows.length})</span>
        ) : null}
        <span className="micro findings-note">Last 30 days, most urgent first</span>
        {problems &&
        (problems.copies_refused_total ?? 0) > (problems.copies_refused?.length ?? 0) ? (
          <span className="micro findings-note">
            Showing {problems.copies_refused?.length ?? 0} of {problems.copies_refused_total}{" "}
            refused copies
          </span>
        ) : null}
      </div>
      {!problems ? (
        <Loading />
      ) : rows.length === 0 ? (
        <p className="empty">No problems in the last 30 days.</p>
      ) : (
        <ul className="findings-list" aria-label="Annex problems">
          {ranked.map(({ r }, n) => (
            <li key={r.key} className="finding">
              <span className="finding-rank" aria-hidden="true">
                {String(n + 1).padStart(2, "0")}
              </span>
              <div className="finding-main">
                <div className="finding-title">
                  <span className="tag" data-tone={r.tone}>
                    {r.word}
                  </span>
                  {r.body}
                </div>
              </div>
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}

export function AnnexPage() {
  const { annexId = "" } = useParams();
  const q = useAnnex(annexId);
  const a = q.data?.annex;
  usePageTitle(a ? a.name : "Annex");
  const patch = usePatchAnnex(annexId);
  const purge = usePurgeAnnexFile(annexId);
  const [confirming, setConfirming] = useState<AnnexFile | null>(null);
  const [days, setDays] = useState<number>(30);
  const [state, setState] = useState("");
  const [path, setPath] = useState("");
  const debounced = useDebounced(path, 250);
  const timeline = useAnnexTimeline(annexId, days);
  const files = useAnnexFiles(annexId, state, debounced);
  const problems = useAnnexProblems(annexId);
  const ids = { days: useId(), state: useId(), path: useId() };

  if (q.isError) {
    return (
      <div className="page">
        <h1>Annex</h1>
        <ErrorCallout error={q.error} />
      </div>
    );
  }
  if (!a) {
    return (
      <div className="page">
        <Loading />
      </div>
    );
  }
  const health = a.filer.health as { ok?: boolean; read_only?: boolean; error?: string } | null;
  const paused = a.state === "paused";
  const present = a.counts.present;
  return (
    <div className="page">
      <PageHead
        crumbs={
          <p className="crumbs">
            <Link to="/annexes">Annexes</Link>
          </p>
        }
        title={a.name}
        question="Is every file in this NAS folder safely copied to Azure?"
        lede={a.unc}
        ledeClassName="mono"
        hero={
          present > 0 ? (
            <HeroMetric
              label="Verified in Azure"
              value={String(Math.floor((a.counts.verified / present) * 100))}
              unit="%"
              sub={`${a.counts.verified.toLocaleString()} of ${present.toLocaleString()} files`}
            />
          ) : null
        }
        actions={
          a.can_manage && a.state !== "retired" ? (
            <button
              type="button"
              className="btn"
              disabled={patch.isPending}
              onClick={() =>
                patch.mutate(
                  { body: { state: paused ? "active" : "paused" }, etag: q.data?.etag ?? "" },
                  { onSuccess: () => void q.refetch() },
                )
              }
            >
              {paused ? "Resume copying" : "Pause copying"}
            </button>
          ) : null
        }
      />
      {patch.isError ? <ErrorCallout error={patch.error} /> : null}
      {a.ruleset_state === "proposed" ? (
        <section className="callout callout-warn" aria-labelledby="annex-proposed">
          <h2 id="annex-proposed">Waiting for approval</h2>
          <p>
            An application proposed this annex. Nothing is read from the NAS or copied until one of
            its Owners or an ArchiveAdmin approves it.{" "}
            {a.can_manage ? (
              <a href="#annex-rules">Review the proposal under Rules, then approve it there.</a>
            ) : null}
          </p>
        </section>
      ) : null}

      <section className="card card-pad" aria-labelledby="annex-head">
        <h2 id="annex-head" className="sr-only">
          Overview
        </h2>
        <dl className="kv">
          <dt>Mode</dt>
          <dd>
            {a.mode === "copy" ? "Copy: every file copied to Azure, the NAS never changed" : a.mode}
          </dd>
          <dt>State</dt>
          <dd>{label(STATE_LABEL, a.state)}</dd>
          <dt>Mount</dt>
          <dd>
            {health === null ? (
              "Not checked yet"
            ) : health.ok ? (
              <span className="badge badge-ok">
                {health.read_only ? "Read-only, readable" : "Readable"}
              </span>
            ) : (
              <span className="badge badge-danger">{health.error ?? "Not readable"}</span>
            )}{" "}
            {a.host_trusted ? (
              <span className="badge badge-warn">Host-trusted (sec=sys)</span>
            ) : null}
            {a.filer.health_checked_at ? (
              <span className="muted"> {formatRelative(a.filer.health_checked_at)}</span>
            ) : null}
          </dd>
          <dt>The NAS shows its ACLs</dt>
          <dd>
            <FilerAcls filer={a.filer} canAnswer={a.can_purge} />
          </dd>
          <dt>Share or export limits access</dt>
          <dd>
            <FilerAccess filer={a.filer} canAnswer={a.can_purge} />
          </dd>
          <dt>Present when the annex started</dt>
          <dd>
            {a.baseline.at
              ? `${a.baseline.files ?? 0} files (${formatBytes(a.baseline.bytes ?? 0)}), ${formatDateTime(a.baseline.at)}`
              : "First scan not finished"}
          </dd>
          <dt>Copies</dt>
          <dd>
            {a.counts.verified} of {a.counts.present} verified in Azure (
            {formatBytes(a.counts.bytes_verified)} of {formatBytes(a.counts.bytes)});{" "}
            {a.counts.pending} waiting
          </dd>
          <dt>Deleted on the NAS</dt>
          <dd>{a.counts.gone} kept in Azure until an ArchiveAdmin purges them</dd>
          <dt>Scans</dt>
          <dd>
            quick {a.last_quick_scan_at ? formatRelative(a.last_quick_scan_at) : "not yet"}, full{" "}
            {a.last_full_scan_at ? formatRelative(a.last_full_scan_at) : "not yet"}, reconciliation{" "}
            {a.last_reconcile_at ? formatRelative(a.last_reconcile_at) : "not yet"}
          </dd>
        </dl>
      </section>

      <section className="card card-pad stack" aria-labelledby="annex-timeline">
        <div className="row">
          <h2 id="annex-timeline">Arrivals and departures</h2>
          <span className="spacer" />
          <Pills>
            <span className="pill">
              <label className="pill-key" htmlFor={ids.days}>
                Window
              </label>
              <select id={ids.days} value={days} onChange={(e) => setDays(Number(e.target.value))}>
                {DAYS.map((n) => (
                  <option key={n} value={n}>
                    Last {n} days
                  </option>
                ))}
              </select>
            </span>
          </Pills>
        </div>
        {timeline.data ? <TimelineChart t={timeline.data} /> : <Loading />}
        <ul className="chart-legend">
          <li>
            <span className="legend-swatch in" /> arrived or changed
          </li>
          <li>
            <span className="legend-swatch out" /> left the NAS
          </li>
          <li>
            <span className="legend-swatch copy" /> copied
          </li>
          <li>
            <span className="legend-swatch avg" /> daily average of arrivals
          </li>
        </ul>
      </section>

      <section className="card card-pad" aria-labelledby="annex-files">
        <div className="row toolbar">
          <h2 id="annex-files">Files</h2>
          <span className="spacer" />
          <Pills label="This table">
            <span className="pill">
              <label className="pill-key" htmlFor={ids.path}>
                Path starts with
              </label>
              <input
                id={ids.path}
                type="search"
                value={path}
                onChange={(e) => setPath(e.target.value)}
              />
            </span>
            <span className="pill">
              <label className="pill-key" htmlFor={ids.state}>
                Show
              </label>
              <select id={ids.state} value={state} onChange={(e) => setState(e.target.value)}>
                <option value="">All</option>
                <option value="present">On the NAS</option>
                <option value="gone">Deleted on the NAS</option>
                <option value="purged">Purged</option>
              </select>
            </span>
          </Pills>
        </div>
        {files.isError ? <ErrorCallout error={files.error} /> : null}
        {purge.isError ? <ErrorCallout error={purge.error} /> : null}
        {!files.data ? (
          <Loading />
        ) : files.data.items.length === 0 ? (
          <p className="empty">No files here that you can read.</p>
        ) : (
          <div className="table-wrap">
            <table className="table" aria-label="Annexed files">
              <thead>
                <tr>
                  <th scope="col">Path</th>
                  <th scope="col" className="col-num">
                    Size
                  </th>
                  <th scope="col">Where</th>
                  <th scope="col">Copy</th>
                  <th scope="col">First seen</th>
                  <th scope="col">Get it back</th>
                  {a.can_purge ? <th scope="col">Purge</th> : null}
                </tr>
              </thead>
              <tbody>
                {files.data.items.map((f) => {
                  const c = COPY_LABEL[f.copy_state] ?? {
                    label: f.copy_state,
                    badge: "badge-outline",
                  };
                  return (
                    <tr key={f.file_id}>
                      <th scope="row" className="mono">
                        {f.path}
                        {f.folders_narrow && f.folders_narrow.length > 0 ? (
                          <div className="muted micro">
                            {/* Decision 63: who may read it is narrowed by these. */}
                            Only for those{" "}
                            {f.folders_narrow.length === 1 ? "this folder" : "these folders"} let
                            through: {f.folders_narrow.map((p) => p || "/").join(", ")}
                          </div>
                        ) : null}
                      </th>
                      <td className="col-num">{formatBytes(f.size)}</td>
                      <td>
                        <Track f={f} />
                        <div className="muted micro">{label(NAS_LABEL, f.nas_state)}</div>
                      </td>
                      <td>
                        <span className={`badge ${c.badge}`}>{c.label}</span>
                      </td>
                      <td>{f.baseline ? "Before the annex" : formatDateTime(f.first_seen)}</td>
                      <td>{canGetBack(f) ? <GetBack file={f} /> : null}</td>
                      {a.can_purge ? (
                        <td>
                          {f.gone_since && !f.purge_requested_at ? (
                            <button
                              type="button"
                              className="btn btn-sm btn-danger"
                              onClick={() => setConfirming(f)}
                            >
                              Purge copy of {f.path}
                            </button>
                          ) : f.purge_requested_at ? (
                            "Purging"
                          ) : null}
                        </td>
                      ) : null}
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        )}
      </section>

      <RulesSection annex={a} etag={q.data?.etag ?? ""} onChanged={() => void q.refetch()} />

      <ProblemsCard problems={problems.data} annexId={annexId} canAct={a.can_purge} />

      <Modal
        open={confirming !== null}
        onClose={() => setConfirming(null)}
        title="Purge the kept copy?"
        description={
          confirming ? (
            <>
              <span className="mono">{confirming.path}</span> was deleted on the NAS. Its copy in
              Azure is the last one: purged, it can&apos;t be brought back.
            </>
          ) : null
        }
        footer={
          <>
            <button type="button" className="btn" onClick={() => setConfirming(null)}>
              Keep it
            </button>
            <button
              type="button"
              className="btn btn-danger"
              disabled={purge.isPending}
              onClick={() => {
                const f = confirming;
                if (!f) return;
                purge.mutate(f.file_id, {
                  onSuccess: () => {
                    setConfirming(null);
                    void files.refetch();
                  },
                });
              }}
            >
              Purge the copy
            </button>
          </>
        }
      >
        {purge.isError ? <ErrorCallout error={purge.error} /> : null}
        <p>WORM retention and legal holds still refuse it, and the refusal is recorded.</p>
      </Modal>
    </div>
  );
}
