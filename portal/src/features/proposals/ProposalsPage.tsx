import { useId } from "react";
import { Link, useSearchParams } from "react-router";
import { useDatasets, useProposals } from "../../api/queries";
import type { components } from "../../api/schema.gen";
import type { PersonRef, ProposalState } from "../../api/types";
import { usePageTitle } from "../../app/usePageTitle";
import { Empty, ErrorCallout, Loading } from "../../components/Feedback";
import { HeroMetric, Pills } from "../../components/PageHead";
import { formatRelative } from "../../lib/format";

export const PROPOSAL_STATE: Record<ProposalState, { label: string; tone: string }> = {
  pending: { label: "Waiting for approval", tone: "badge-accent" },
  approved: { label: "Approved", tone: "badge-ok" },
  rejected: { label: "Rejected", tone: "badge-danger" },
  withdrawn: { label: "Withdrawn", tone: "" },
  superseded: { label: "Superseded", tone: "" },
};

type ApiClientRef = components["schemas"]["ApiClientRef"];

/**
 * Who sent a proposal. An application is always "API": with its registered
 * client's name and owner when known (Settings, API clients), else its app id.
 */
export function proposerText(who: PersonRef, client?: ApiClientRef | null): string {
  if (who.kind === "application") {
    if (client) {
      const owner = client.owner.display_name ?? client.owner.app_id ?? "unknown";
      return `From: API · ${client.display_name} · owner ${owner}`;
    }
    return `From: API · ${who.app_id ?? "unknown app"}`;
  }
  return who.display_name ?? (who.kind === "system" ? "System" : "Unknown");
}

export function Proposer({ who, client }: { who: PersonRef; client?: ApiClientRef | null }) {
  if (who.kind === "application") {
    return (
      <span className="row" style={{ gap: 6 }}>
        <span className="badge badge-info">API</span>
        {proposerText(who, client)}
      </span>
    );
  }
  return (
    <span className="row" style={{ gap: 6 }}>
      <span className="badge badge-outline">{who.kind === "system" ? "System" : "Person"}</span>
      {proposerText(who, client)}
    </span>
  );
}

export function ProposalsPage() {
  usePageTitle("Proposals");
  const [params, setParams] = useSearchParams();
  const datasetId = params.get("dataset") ?? "";
  const state = (params.get("state") ?? "pending") as ProposalState | "all";
  const datasets = useDatasets();
  const proposals = useProposals(datasetId || undefined, state === "all" ? undefined : state);
  const dsId = useId();
  const stId = useId();
  const datasetName = (id: string) =>
    datasets.data?.items.find((d) => d.dataset_id === id)?.name ?? `Dataset ${id}`;
  const update = (key: string, value: string) => {
    const next = new URLSearchParams(params);
    if (value) next.set(key, value);
    else next.delete(key);
    setParams(next);
  };

  return (
    <div className="page">
      <div className="page-head">
        <div>
          <h1>Policy proposals</h1>
          <p className="page-question">Which policy changes are waiting for a decision?</p>
          <p className="lede">
            A proposal changes nothing until an ArchiveAdmin who didn't propose it approves it.
          </p>
        </div>
        {datasets.data ? (
          <HeroMetric
            label="Waiting"
            value={String(datasets.data.items.reduce((n, d) => n + (d.pending_proposals ?? 0), 0))}
            sub="proposals across your datasets"
          />
        ) : null}
      </div>
      <div className="toolbar">
        <Pills label="Filter">
          <span className="pill">
            <label htmlFor={stId} className="pill-key">
              Show
            </label>
            <select id={stId} value={state} onChange={(e) => update("state", e.target.value)}>
              <option value="pending">Waiting for approval</option>
              <option value="approved">Approved</option>
              <option value="rejected">Rejected</option>
              <option value="withdrawn">Withdrawn</option>
              <option value="all">All</option>
            </select>
          </span>
          <span className="pill">
            <label htmlFor={dsId} className="pill-key">
              for
            </label>
            <select id={dsId} value={datasetId} onChange={(e) => update("dataset", e.target.value)}>
              <option value="">every dataset</option>
              {(datasets.data?.items ?? []).map((d) => (
                <option key={d.dataset_id} value={d.dataset_id}>
                  {d.name}
                </option>
              ))}
            </select>
          </span>
        </Pills>
      </div>
      {proposals.isPending ? (
        <Loading />
      ) : proposals.isError ? (
        <ErrorCallout error={proposals.error} />
      ) : proposals.data.items.length === 0 ? (
        <div className="table-wrap">
          <Empty title="Nothing here">
            <p>No proposals match.</p>
          </Empty>
        </div>
      ) : (
        <div className="table-wrap">
          <table className="table" aria-label="Proposals">
            <thead>
              <tr>
                <th scope="col">Proposal</th>
                <th scope="col">Dataset</th>
                <th scope="col">From</th>
                <th scope="col">State</th>
                <th scope="col">Sent</th>
              </tr>
            </thead>
            <tbody>
              {proposals.data.items.map((p) => (
                <tr key={p.proposal_id}>
                  <td>
                    <span className="cell-name">
                      <Link to={`/proposals/${p.proposal_id}`}>
                        {p.rationale ? truncate(p.rationale, 90) : `Proposal ${p.proposal_id}`}
                      </Link>
                    </span>
                  </td>
                  <td>{datasetName(p.dataset_id)}</td>
                  <td>
                    <Proposer who={p.proposed_by} client={p.api_client ?? null} />
                  </td>
                  <td>
                    <span className="row" style={{ gap: 4 }}>
                      <span className={`badge ${PROPOSAL_STATE[p.state].tone}`}>
                        {PROPOSAL_STATE[p.state].label}
                      </span>
                      {p.stale ? <span className="badge badge-warn">Out of date</span> : null}
                    </span>
                  </td>
                  <td className="num">{formatRelative(p.created_at)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}

function truncate(text: string, n: number): string {
  return text.length > n ? `${text.slice(0, n - 1)}…` : text;
}
