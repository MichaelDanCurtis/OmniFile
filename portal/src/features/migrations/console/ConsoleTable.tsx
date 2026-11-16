import { Fragment, useState } from "react";
import { Link } from "react-router";
import { Empty, ErrorCallout, Loading } from "../../../components/Feedback";
import { Tip } from "../../../components/Tip";
import {
  formatBytes,
  formatDateTime,
  formatRelative,
  formatShortDateTime,
  formatShortDay,
} from "../../../lib/format";
import { copierLabel, migrationStateLabel, runStateLabel } from "../labels";
import { useWork, type ConsoleRow } from "./consoleApi";
import { agentStatusBadge, healthBadge, healthLabel, rateLabel, stuckNotes } from "./consoleLabels";

/** Who works on one migration: its Storage Mover job and agents, edge, waves and checks. */
export function WorkPanel({ migrationId }: { migrationId: string }) {
  const work = useWork(migrationId);
  if (work.isPending) return <Loading label="Loading who works on it…" />;
  if (work.isError) return <ErrorCallout error={work.error} />;
  const w = work.data;
  const sm = w.storage_mover;
  return (
    <div className="console-work">
      <section aria-label="Copier">
        <h3>Copier</h3>
        {sm ? (
          <>
            <dl className="kv">
              <dt>Storage Mover job</dt>
              <dd className="mono">{sm.job_ref ?? "Not made yet"}</dd>
              <dt>Last run</dt>
              <dd>
                {sm.last_run
                  ? `${(sm.last_run as { run_ref?: string }).run_ref ?? ""} · ${runStateLabel(
                      (sm.last_run as { state?: string | null }).state,
                    )}`
                  : "None yet"}
              </dd>
            </dl>
            {sm.agents.length === 0 ? (
              <p className="muted">
                No Storage Mover agent is listed. A cloud-to-cloud job (S3 through the Azure Arc
                multicloud connector) runs without one.
              </p>
            ) : (
              <table
                className="table"
                aria-label={`Storage Mover agents for migration ${migrationId}`}
              >
                <thead>
                  <tr>
                    <th scope="col">Agent</th>
                    <th scope="col">Status</th>
                    <th scope="col">Host</th>
                    <th scope="col">Version</th>
                    <th scope="col">Last heard</th>
                  </tr>
                </thead>
                <tbody>
                  {sm.agents.map((a) => (
                    <tr key={a.name}>
                      <td className="mono">{a.name}</td>
                      <td>
                        <span className={agentStatusBadge(a.status, a.stale)}>
                          {a.stale ? "Not read lately" : (a.status ?? "Unknown")}
                        </span>
                      </td>
                      <td className="mono">{a.host ?? "—"}</td>
                      <td>{a.version ?? "—"}</td>
                      <td>{formatRelative(a.last_status_at ?? a.polled_at)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
          </>
        ) : (
          <p>An external copier works through the API; OmniFile only verifies and cuts over.</p>
        )}
      </section>
      <section aria-label="Edge">
        <h3>Edge</h3>
        {w.edge ? (
          <dl className="kv">
            <dt>Edge</dt>
            <dd>
              {w.edge.name}{" "}
              <span className={healthBadge(w.edge.status === "ok" ? "ok" : "attention")}>
                {w.edge.status}
              </span>
            </dd>
            <dt>Last heartbeat</dt>
            <dd>{formatRelative(w.edge.last_heartbeat_at)}</dd>
            <dt>Version</dt>
            <dd>{w.edge.software_version ?? "—"}</dd>
          </dl>
        ) : (
          <p className="muted">The dataset has no edge.</p>
        )}
      </section>
      <section aria-label="Restore waves">
        <h3>S3 restore waves</h3>
        {w.restore_waves.length === 0 ? (
          <p className="muted">None: no archived objects needed restoring so far.</p>
        ) : (
          <table className="table" aria-label={`Restore waves for migration ${migrationId}`}>
            <thead>
              <tr>
                <th scope="col">Batch job</th>
                <th scope="col">State</th>
                <th scope="col" className="col-num">
                  Restored / objects
                </th>
                <th scope="col">First copy expires</th>
              </tr>
            </thead>
            <tbody>
              {w.restore_waves.map((r) => (
                <tr key={r.wave_id}>
                  <td className="mono">{r.job_id ?? "—"}</td>
                  <td>
                    {r.state}
                    {r.failure ? <div className="muted">{r.failure}</div> : null}
                  </td>
                  <td className="col-num">
                    {r.restored.toLocaleString()} / {r.objects.toLocaleString()}
                  </td>
                  <td>{formatDateTime(r.first_expiry)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
        {w.checksum_jobs.length > 0 ? (
          <p className="muted">
            {w.checksum_jobs.filter((c) => c.state === "running").length} S3 checksum jobs running,{" "}
            {w.checksum_jobs.length} in all.
          </p>
        ) : null}
      </section>
      <section aria-label="Verification">
        <h3>Verification</h3>
        <dl className="kv">
          <dt>Verified</dt>
          <dd>
            {w.verification.verified.toLocaleString()} of{" "}
            {w.verification.total === null ? "?" : w.verification.total.toLocaleString()} files,{" "}
            {formatBytes(w.verification.bytes.verified)} of{" "}
            {formatBytes(w.verification.bytes.total)}
          </dd>
          <dt>Still in an S3 archive class</dt>
          <dd>{w.verification.archived_waiting.toLocaleString()}</dd>
          <dt>Bucket events applied to</dt>
          <dd>{formatDateTime(w.verification.journal_checked_through)}</dd>
        </dl>
      </section>
    </div>
  );
}

/** Every migration: phase, progress, throughput, ETA, health and the next wave. */
export function ConsoleTable({
  rows,
  loading,
  error,
}: {
  rows: ConsoleRow[] | undefined;
  loading: boolean;
  error: unknown;
}) {
  const [open, setOpen] = useState<string | null>(null);
  if (error) return <ErrorCallout error={error} />;
  if (loading || !rows) return <Loading />;
  if (rows.length === 0) {
    return (
      <Empty title="Nothing is being moved">
        <p className="muted">Start one with New migration, or plan it in stages with New plan.</p>
      </Empty>
    );
  }
  return (
    <div className="table-wrap card">
      <table className="table table-dense console-table" aria-label="Prefixes being moved">
        <thead>
          <tr>
            <th scope="col">
              <span className="sr-only">Details</span>
            </th>
            <th scope="col">Dataset and prefix</th>
            <th scope="col">Phase</th>
            <th scope="col">Verified</th>
            <th scope="col" className="col-num">
              Failed / excluded
            </th>
            <th scope="col">Throughput</th>
            <th scope="col">ETA</th>
            <th scope="col">Health</th>
            <th scope="col">Next wave</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((m) => {
            const total = m.files.total ?? 0;
            const expanded = open === m.migration_id;
            return (
              <Fragment key={m.migration_id}>
                <tr>
                  <td>
                    <button
                      type="button"
                      className="icon-btn"
                      aria-expanded={expanded}
                      aria-controls={`work-${m.migration_id}`}
                      aria-label={`${expanded ? "Hide" : "Show"} who works on ${m.prefix || "the whole source"}`}
                      onClick={() => setOpen(expanded ? null : m.migration_id)}
                    >
                      {expanded ? "−" : "+"}
                    </button>
                  </td>
                  <td>
                    <div className="cell-line">
                      <Link className="mono" to={`/migrations/${m.migration_id}`}>
                        {m.prefix || "(whole source)"}
                      </Link>
                    </div>
                    <span className="sr-only">
                      {m.dataset_name}, <span>{copierLabel(m.copier_kind)}</span>
                    </span>
                  </td>
                  <td>
                    <div className="cell-line cell-line-sm">{migrationStateLabel(m.state)}</div>
                  </td>
                  <td>
                    <div className="cell-line cell-meter">
                      <progress
                        max={Math.max(1, total)}
                        value={m.files.verified}
                        aria-label={`${m.files.verified} of ${total} files verified`}
                      />
                      <Tip
                        label="Progress"
                        tip={`${m.files.verified.toLocaleString()} of ${total.toLocaleString()} files, ${formatBytes(m.bytes.verified)} of ${formatBytes(m.bytes.total)}`}
                      >
                        {m.files.verified.toLocaleString()} / {total.toLocaleString()} ·{" "}
                        {formatBytes(m.bytes.verified)}
                      </Tip>
                    </div>
                    <span className="sr-only">files verified, of {formatBytes(m.bytes.total)}</span>
                  </td>
                  <td className="col-num">
                    {m.failing_now.toLocaleString()} / {m.files.excluded.toLocaleString()}
                  </td>
                  <td>
                    <div className="cell-line">
                      {m.throughput ? (
                        <>
                          <span>{rateLabel(m.throughput.bytes_per_s)}</span>
                          <span className="sr-only">
                            , {m.throughput.files_per_h.toLocaleString()} files/h
                          </span>
                        </>
                      ) : (
                        <>
                          <span aria-hidden="true">—</span>
                          <span className="sr-only">None in the last hour</span>
                        </>
                      )}
                    </div>
                  </td>
                  <td>
                    <div className="cell-line">
                      <Tip
                        label="How the ETA is worked out"
                        tip={`${m.eta.basis}${m.counted_at ? `; counted ${formatRelative(m.counted_at)}` : ""}`}
                      >
                        {m.eta.at ? formatShortDateTime(m.eta.at) : <span>No time promised</span>}
                      </Tip>
                    </div>
                  </td>
                  <td>
                    <span className={healthBadge(m.health)}>{healthLabel(m.health)}</span>
                    {stuckNotes(m.stuck_copies).map((note) => (
                      <div key={note} className="muted">
                        {note}
                      </div>
                    ))}
                  </td>
                  <td>
                    {m.next_wave ? (
                      <div className="cell-line">
                        <Tip label="Starts" tip={formatDateTime(m.next_wave.start_at)}>
                          <Link to={`/migrations/plans/${m.next_wave.plan_id}`}>
                            {m.next_wave.name}
                            <span className="muted"> · {formatShortDay(m.next_wave.start_at)}</span>
                          </Link>
                        </Tip>
                      </div>
                    ) : (
                      "—"
                    )}
                  </td>
                </tr>
                {expanded ? (
                  <tr id={`work-${m.migration_id}`}>
                    <td colSpan={9}>
                      <p className="console-row-detail">
                        {m.dataset_name} · {copierLabel(m.copier_kind)}
                        {m.throughput
                          ? ` · ${m.throughput.files_per_h.toLocaleString()} files an hour`
                          : ""}{" "}
                        · ETA: {m.eta.basis}
                        {m.counted_at ? `, counted ${formatRelative(m.counted_at)}` : ""}
                      </p>
                      <WorkPanel migrationId={m.migration_id} />
                    </td>
                  </tr>
                ) : null}
              </Fragment>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}
