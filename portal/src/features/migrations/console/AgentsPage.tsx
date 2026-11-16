import { Link } from "react-router";
import { PageHead } from "../../../components/PageHead";
import { MigrationTabs } from "./MigrationTabs";
import { useSession } from "../../../app/session";
import { usePageTitle } from "../../../app/usePageTitle";
import { Callout, ErrorCallout, Loading } from "../../../components/Feedback";
import { formatRelative } from "../../../lib/format";
import { isArchiveAdmin } from "../../../lib/permissions";
import { copierLabel, migrationStateLabel, runStateLabel } from "../labels";
import { useAgents } from "./consoleApi";
import { agentStatusBadge } from "./consoleLabels";

/** Which agent, edge and job runs what, across every migration under way. */
/** A copy run's counters in words: "files copied 1,200, bytes 3,400". */
function counters(c: Record<string, number>): string {
  return Object.entries(c)
    .map(([k, v]) => `${k.replace(/_/g, " ")} ${v.toLocaleString()}`)
    .join(", ");
}

export function AgentsPage() {
  usePageTitle("Migration agents");
  const { me } = useSession();
  const admin = isArchiveAdmin(me);
  const agents = useAgents(admin);
  if (!admin) {
    return (
      <div className="page">
        <h1>Migration agents</h1>
        <ErrorCallout error={new Error("Migrations are for ArchiveAdmin.")} />
      </div>
    );
  }
  return (
    <div className="page">
      <PageHead
        title="Migration agents"
        question="Who is copying, and is every agent online?"
        lede="As the worker last listed them, every 5 minutes."
      />
      <MigrationTabs />
      {agents.isError ? <ErrorCallout error={agents.error} /> : null}
      {!agents.data ? (
        agents.isError ? null : (
          <Loading />
        )
      ) : (
        <>
          <section aria-labelledby="ag-agents" className="stack">
            <h2 id="ag-agents">Storage Mover agents</h2>
            {agents.data.agents.length === 0 ? (
              <Callout tone="info" title="No agents listed">
                The worker has listed no Storage Mover agents. Cloud-to-cloud jobs (S3 through the
                Azure Arc multicloud connector) run without one; agents appear here once one is
                registered.
              </Callout>
            ) : (
              <div className="table-wrap card">
                <table className="table" aria-label="Storage Mover agents">
                  <thead>
                    <tr>
                      <th scope="col">Agent</th>
                      <th scope="col">Status</th>
                      <th scope="col">Host or site</th>
                      <th scope="col">Version</th>
                      <th scope="col">Last heard</th>
                      <th scope="col">Last error</th>
                    </tr>
                  </thead>
                  <tbody>
                    {agents.data.agents.map((a) => (
                      <tr key={a.name}>
                        <td className="mono">{a.name}</td>
                        <td>
                          <span className={agentStatusBadge(a.status, a.stale)}>
                            {a.stale ? "Not read lately" : (a.status ?? "Unknown")}
                          </span>
                        </td>
                        <td>
                          <span className="mono">{a.host ?? "—"}</span>
                          {a.arc_resource_id ? (
                            <div className="muted micro">
                              {a.arc_resource_id.split("/").slice(-1)[0]}
                            </div>
                          ) : null}
                        </td>
                        <td>{a.version ?? "—"}</td>
                        <td>{formatRelative(a.last_status_at ?? a.polled_at)}</td>
                        <td>{a.error ?? "—"}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </section>
          <section aria-labelledby="ag-jobs" className="stack">
            <h2 id="ag-jobs">Jobs under way</h2>
            {agents.data.jobs.length === 0 ? (
              <p className="muted">No migration is under way.</p>
            ) : (
              <div className="table-wrap card">
                <table className="table table-dense" aria-label="Jobs under way">
                  <thead>
                    <tr>
                      <th scope="col">Migration</th>
                      <th scope="col">Phase</th>
                      <th scope="col">Copier and job</th>
                      <th scope="col">Run</th>
                      <th scope="col">Edge</th>
                      <th scope="col" className="col-num">
                        Restore waves / checksum jobs
                      </th>
                    </tr>
                  </thead>
                  <tbody>
                    {agents.data.jobs.map((j) => (
                      <tr key={j.migration_id}>
                        <td title={j.dataset_name}>
                          <div className="cell-line">
                            <Link className="mono" to={`/migrations/${j.migration_id}`}>
                              {j.prefix || "(whole source)"}
                            </Link>
                          </div>
                          <span className="sr-only">{j.dataset_name}</span>
                        </td>
                        <td>{migrationStateLabel(j.state)}</td>
                        <td className="nowrap" title={j.job_ref ?? undefined}>
                          {copierLabel(j.copier_kind)}
                          <span className="sr-only">, job {j.job_ref ?? "—"}</span>
                        </td>
                        <td className="nowrap" title={counters(j.run_counters) || undefined}>
                          {runStateLabel(j.run_state)}
                          {Object.keys(j.run_counters).length ? (
                            <span className="sr-only">, {counters(j.run_counters)}</span>
                          ) : null}
                        </td>
                        <td>
                          {j.edge ? (
                            <>
                              {j.edge.name} <span className="muted">({j.edge.status})</span>
                            </>
                          ) : (
                            "—"
                          )}
                        </td>
                        <td className="col-num">
                          {j.restore_waves_running} / {j.checksum_jobs_running}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </section>
        </>
      )}
    </div>
  );
}
