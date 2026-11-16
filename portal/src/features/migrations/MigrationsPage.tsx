import { Link } from "react-router";
import { useDatasets, useSources } from "../../api/queries";
import { useSession } from "../../app/session";
import { usePageTitle } from "../../app/usePageTitle";
import { Empty, ErrorCallout, Loading } from "../../components/Feedback";
import { HeroMetric, PageHead } from "../../components/PageHead";
import { isArchiveAdmin } from "../../lib/permissions";
import { AttentionPanel } from "./console/AttentionPanel";
import { ConsoleTable } from "./console/ConsoleTable";
import { useConsole, type ConsoleRow } from "./console/consoleApi";
import { MigrationTabs } from "./console/MigrationTabs";
import { sourceStateLabel } from "./labels";
import "./console/console.css";

/** The share of all bytes being moved that is copied and checked: the page's hero number. */
function verifiedShare(rows: ConsoleRow[]): { pct: string; sub: string } | null {
  const total = rows.reduce((n, m) => n + m.bytes.total, 0);
  if (rows.length === 0 || total <= 0) return null;
  const done = rows.reduce((n, m) => n + m.bytes.verified, 0);
  return {
    pct: String(Math.floor((done / total) * 100)),
    sub: `of the bytes in ${rows.length} migration${rows.length === 1 ? "" : "s"}`,
  };
}

export function MigrationsPage() {
  usePageTitle("Migrations");
  const { me } = useSession();
  const admin = isArchiveAdmin(me);
  const sources = useSources(admin);
  const board = useConsole(admin);
  const datasets = useDatasets(admin);
  const datasetName = (id: string) =>
    datasets.data?.items.find((d) => d.dataset_id === id)?.name ?? `dataset ${id}`;

  if (!admin) {
    return (
      <div className="page">
        <h1>Migrations</h1>
        <ErrorCallout error={new Error("Migrations are for ArchiveAdmin.")} />
      </div>
    );
  }
  const share = board.data ? verifiedShare(board.data.migrations) : null;
  return (
    <div className="page">
      <PageHead
        title="Migrations"
        question="How far has each prefix moved, and what needs a person?"
        lede="Files switch over only once checked byte for byte. Expand a row for its detail."
        hero={
          share ? <HeroMetric label="Verified" value={share.pct} unit="%" sub={share.sub} /> : null
        }
        actions={
          <>
            <Link className="btn" to="/migrations/plans/new">
              New plan
            </Link>
            <Link className="btn btn-primary" to="/migrations/new">
              New migration
            </Link>
          </>
        }
      />
      <MigrationTabs />

      {board.data ? <AttentionPanel items={board.data.attention} /> : null}

      <section aria-labelledby="mig-loc" className="stack">
        <h2 id="mig-loc">Prefixes being moved</h2>
        <ConsoleTable rows={board.data?.migrations} loading={board.isPending} error={board.error} />
      </section>

      <section aria-labelledby="mig-src">
        <h2 id="mig-src">Sources</h2>
        {sources.isError ? <ErrorCallout error={sources.error} /> : null}
        {!sources.data ? (
          sources.isError ? null : (
            <Loading />
          )
        ) : sources.data.items.length === 0 ? (
          <Empty title="No sources registered" />
        ) : (
          <div className="table-wrap card">
            <table className="table table-dense" aria-label="Sources">
              <thead>
                <tr>
                  <th scope="col">Dataset</th>
                  <th scope="col">Bucket and prefix</th>
                  <th scope="col">Folder</th>
                  <th scope="col">State</th>
                  <th scope="col" className="col-num">
                    Files catalogued
                  </th>
                  <th scope="col">Last problem</th>
                  <th scope="col">
                    <span className="sr-only">Next step</span>
                  </th>
                </tr>
              </thead>
              <tbody>
                {sources.data.items.map((s) => (
                  <tr key={s.source_id}>
                    <td>
                      <div className="cell-line" title={datasetName(s.dataset_id)}>
                        {datasetName(s.dataset_id)}
                      </div>
                    </td>
                    <td className="mono">
                      {s.container}/{s.prefix}
                    </td>
                    <td className="mono">/{s.path}</td>
                    <td>
                      {sourceStateLabel(s.state)}
                      {s.kind === "adopt" ? <div className="muted">adopted in place</div> : null}
                    </td>
                    <td className="col-num">{s.counts.files_created}</td>
                    <td>
                      <div className="cell-line" title={s.last_error ?? undefined}>
                        {s.last_error ?? "—"}
                      </div>
                    </td>
                    <td>
                      {/* The server counts each source's migrations (not just this page's). */}
                      {s.kind === "source" && s.active_migrations === 0 ? (
                        <Link
                          className="btn btn-sm"
                          to={`/migrations/new?source=${s.source_id}`}
                          aria-label={`Start the migration of ${s.container}/${s.prefix}`}
                        >
                          Start migration
                        </Link>
                      ) : null}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </section>
    </div>
  );
}
