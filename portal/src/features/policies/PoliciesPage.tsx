import { Link } from "react-router";
import { useDatasets } from "../../api/queries";
import type { Dataset } from "../../api/types";
import { useSession } from "../../app/session";
import { usePageTitle } from "../../app/usePageTitle";
import { Empty, ErrorCallout, Loading } from "../../components/Feedback";
import { backendLabel, migrationLabel, pluralize } from "../../lib/format";
import { isArchiveAdmin } from "../../lib/permissions";

/** A proportional sketch of the active schedule (drawing only; numbers come from the server preview). */
function MiniStrata({ policy }: { policy: Dataset["active_policy"] }) {
  if (!policy || policy.mode === "m1" || policy.archive_day == null) {
    return <div className="mini-strata" aria-hidden="true" data-empty="" />;
  }
  const horizon = Math.max(400, policy.archive_day + 30);
  const archive = (policy.archive_day / horizon) * 100;
  return (
    <div className="mini-strata" aria-hidden="true">
      <i data-tier={policy.mode === "smart" ? "smart" : "hot"} style={{ width: `${archive}%` }} />
      <i data-tier="archive" style={{ width: `${100 - archive}%` }} />
    </div>
  );
}

function summary(d: Dataset): string {
  const p = d.active_policy;
  if (!p) return "No policy yet";
  if (p.mode === "m1") return `Version ${p.version} · M1 policy (no tiers)`;
  return `Version ${p.version} · ${p.mode === "smart" ? "Smart" : "Scheduled"} · Archive after ${p.archive_day ?? "?"} days`;
}

export function PoliciesPage() {
  usePageTitle("Policies");
  const { me } = useSession();
  const datasets = useDatasets();
  return (
    <div className="page">
      <div className="page-head">
        <div>
          <h1>Policies</h1>
          <p className="page-question">When do files move to cheaper tiers, and when to Archive?</p>
          <p className="lede">
            {isArchiveAdmin(me)
              ? "One policy per dataset. You can change them."
              : "One policy per dataset. You can propose changes; an ArchiveAdmin approves them."}
          </p>
        </div>
      </div>
      {datasets.isPending ? (
        <Loading />
      ) : datasets.isError ? (
        <ErrorCallout error={datasets.error} />
      ) : datasets.data.items.length === 0 ? (
        <Empty title="No datasets yet" />
      ) : (
        <ul className="share-cards" aria-label="Datasets">
          {datasets.data.items.map((d) => (
            <li key={d.dataset_id}>
              <Link className="card share-card" to={`/policies/${d.dataset_id}`}>
                <span className="row">
                  <strong className="share-card-name">{d.name}</strong>
                  {d.migration_state && d.migration_state !== "steady" ? (
                    <span className="badge badge-info">{migrationLabel(d.migration_state)}</span>
                  ) : null}
                  {d.pending_proposals ? (
                    <span className="badge badge-accent">
                      {pluralize(d.pending_proposals, "proposal")}
                    </span>
                  ) : null}
                </span>
                <span className="muted">
                  {backendLabel(d.backend_kind)} ·{" "}
                  {d.storage_layout === "two_account"
                    ? "two accounts"
                    : d.storage_layout === "single_account"
                      ? "one account"
                      : d.storage_layout}{" "}
                  · {pluralize(d.shares.length, "share")}
                  {d.shares.some((s) => s.worm_enabled) ? " · WORM" : ""}
                </span>
                <MiniStrata policy={d.active_policy} />
                <span>{summary(d)}</span>
              </Link>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
