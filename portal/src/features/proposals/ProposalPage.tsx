import { useId, useState } from "react";
import { Link, useParams } from "react-router";
import { ApiError } from "../../api/client";
import {
  useActivePolicy,
  useDatasets,
  useDecideProposal,
  useProposal,
  useShares,
} from "../../api/queries";
import { ON_OPEN_LABEL } from "../shares/SharesPage";
import { useSession } from "../../app/session";
import { usePageTitle } from "../../app/usePageTitle";
import { Callout, ErrorCallout, Loading } from "../../components/Feedback";
import { useToast } from "../../components/Toasts";
import { formatDateTime } from "../../lib/format";
import { isArchiveAdmin } from "../../lib/permissions";
import { PreviewPane } from "../policies/PreviewPane";
import { fieldLabel, showValue } from "../policies/policyForm";
import { PROPOSAL_STATE, Proposer } from "./ProposalsPage";

export function ProposalPage() {
  const { proposalId = "" } = useParams();
  const { me } = useSession();
  const proposal = useProposal(proposalId);
  const datasets = useDatasets();
  const shares = useShares();
  const active = useActivePolicy(proposal.data?.dataset_id);
  const decide = useDecideProposal(proposalId);
  const toast = useToast();
  const [note, setNote] = useState("");
  const [acceptStale, setAcceptStale] = useState(false);
  const noteId = useId();
  usePageTitle(`Proposal ${proposalId}`);

  if (proposal.isPending) return <Loading />;
  if (proposal.isError) {
    return (
      <div className="page">
        <ErrorCallout error={proposal.error} />
      </div>
    );
  }
  const p = proposal.data;
  const dataset = datasets.data?.items.find((d) => d.dataset_id === p.dataset_id);
  const admin = isArchiveAdmin(me);
  const pending = p.state === "pending";
  // "What users see" answers travel with the proposal and reach the shares only on approval.
  const overrides = p.share_overrides ?? [];
  // Recognise my own proposal by identity id; the server still decides
  // (403 forbidden on withdraw, self_approval on approve).
  const mine =
    p.proposed_by.kind === "identity" &&
    !!me.identity_id &&
    p.proposed_by.identity_id === me.identity_id;
  const decideError = decide.approve.error ?? decide.reject.error ?? decide.withdraw.error;
  const busy = decide.approve.isPending || decide.reject.isPending || decide.withdraw.isPending;

  return (
    <div className="page">
      <nav aria-label="Breadcrumb">
        <ol className="crumbs">
          <li>
            <Link to="/proposals">Proposals</Link>
          </li>
          <li>
            <span aria-current="page">Proposal {p.proposal_id}</span>
          </li>
        </ol>
      </nav>
      <div className="page-head">
        <div>
          <h1>
            Change to{" "}
            {dataset ? (
              <Link to={`/policies/${dataset.dataset_id}`}>{dataset.name}</Link>
            ) : (
              `dataset ${p.dataset_id}`
            )}
          </h1>
          <p className="page-question">What would this change do, and should it be approved?</p>
          <div className="lede row">
            <span className={`badge ${PROPOSAL_STATE[p.state].tone}`}>
              {PROPOSAL_STATE[p.state].label}
            </span>
            {p.proposed_by.kind === "application" ? null : <span>from</span>}
            <Proposer who={p.proposed_by} client={p.api_client ?? null} />
            <span>· {formatDateTime(p.created_at)}</span>
            <span>· based on {p.base_version ? `version ${p.base_version}` : "no policy"}</span>
          </div>
        </div>
      </div>

      <div className="editor">
        <div className="editor-form">
          {pending ? (
            <Callout title="Nothing has changed yet">
              This is a suggestion. It becomes the policy only if an ArchiveAdmin who didn't propose
              it approves it.
            </Callout>
          ) : null}
          {p.stale && pending ? (
            <Callout tone="warn" title="The policy changed since this was proposed">
              It was based on version {p.base_version ?? "none"}; version{" "}
              {active.data?.version ?? "?"} is active now. The differences below are against the
              active version.
            </Callout>
          ) : null}
          {p.rationale ? (
            <section className="card card-pad stack">
              <h2>Why</h2>
              <blockquote className="rationale">{p.rationale}</blockquote>
            </section>
          ) : null}
          <section className="card stack" aria-labelledby="diff-title">
            <div className="card-head">
              <h2 id="diff-title">What would change</h2>
            </div>
            {(p.diff?.length ?? 0) > 0 || overrides.length > 0 ? (
              <table className="table diff" aria-labelledby="diff-title">
                <thead>
                  <tr>
                    <th scope="col">Setting</th>
                    <th scope="col">Active{active.data ? ` (v${active.data.version})` : ""}</th>
                    <th scope="col">Proposed</th>
                  </tr>
                </thead>
                <tbody>
                  {(p.diff ?? []).map((d) => (
                    <tr key={d.field}>
                      <th scope="row">{fieldLabel(d.field)}</th>
                      <td>
                        <span className="was">{showValue(d.active)}</span>
                      </td>
                      <td>
                        <span className="now">{showValue(d.proposed)}</span>
                      </td>
                    </tr>
                  ))}
                  {overrides.map((o) => {
                    const share = shares.data?.items.find((x) => x.share_id === o.share_id);
                    const now = share
                      ? onOpenText(share.on_open, share.wait_threshold_minutes ?? null)
                      : "—";
                    return (
                      <tr key={`share-${o.share_id}`}>
                        <th scope="row">
                          What people see on {share?.name ?? `share ${o.share_id}`}
                        </th>
                        <td>
                          <span className="was">{now}</span>
                        </td>
                        <td>
                          <span className="now">
                            {o.on_open
                              ? onOpenText(o.on_open, o.wait_threshold_minutes ?? null)
                              : now}
                          </span>
                        </td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            ) : (
              <p className="card-body muted">The proposal is the same as the active policy.</p>
            )}
          </section>

          {!pending ? (
            <section className="card card-pad stack">
              <h2>Decision</h2>
              <p>
                {PROPOSAL_STATE[p.state].label}
                {p.decided_by ? ` by ${p.decided_by.display_name ?? "someone"}` : ""}
                {p.decided_at ? ` on ${formatDateTime(p.decided_at)}` : ""}.
              </p>
              {p.decision_note ? (
                <blockquote className="rationale">{p.decision_note}</blockquote>
              ) : null}
            </section>
          ) : admin ? (
            <section className="card card-pad stack" aria-labelledby="decide-title">
              <h2 id="decide-title">Decide</h2>
              <div className="field">
                <label className="field-label" htmlFor={noteId}>
                  Note <span className="muted">(needed to reject)</span>
                </label>
                <textarea
                  id={noteId}
                  value={note}
                  maxLength={4000}
                  onChange={(e) => setNote(e.target.value)}
                />
              </div>
              {p.stale ? (
                <label className="check-row">
                  <input
                    type="checkbox"
                    checked={acceptStale}
                    onChange={(e) => setAcceptStale(e.target.checked)}
                  />
                  <span>
                    Approve it anyway, replacing version {active.data?.version ?? "?"}
                    <span className="field-hint">It was written against an older version.</span>
                  </span>
                </label>
              ) : null}
              {mine ? (
                <p className="field-hint" role="note">
                  You proposed this, so another ArchiveAdmin has to approve it. You can still reject
                  or withdraw it.
                </p>
              ) : null}
              <div className="row">
                {mine ? null : (
                  <button
                    type="button"
                    className="btn btn-primary"
                    disabled={
                      busy || (p.stale === true && !acceptStale) || p.preview?.valid === false
                    }
                    onClick={() =>
                      decide.approve.mutate(
                        {
                          ...(note.trim() ? { note: note.trim() } : {}),
                          ...(p.stale ? { accept_stale: acceptStale } : {}),
                        },
                        {
                          onSuccess: (result) =>
                            toast(`Approved. Version ${result.policy.version} is now active.`),
                        },
                      )
                    }
                  >
                    Approve and make it active
                  </button>
                )}
                <button
                  type="button"
                  className="btn"
                  disabled={busy || !note.trim()}
                  onClick={() =>
                    decide.reject.mutate(note.trim(), {
                      onSuccess: () => toast("Proposal rejected."),
                    })
                  }
                >
                  Reject
                </button>
                {mine ? (
                  <button
                    type="button"
                    className="btn btn-ghost"
                    disabled={busy}
                    onClick={() =>
                      decide.withdraw.mutate(undefined, {
                        onSuccess: () => toast("Proposal withdrawn."),
                      })
                    }
                  >
                    Withdraw
                  </button>
                ) : null}
              </div>
              {p.preview?.valid === false ? (
                <p className="field-error">
                  This proposal has errors (see Checks), so it can't be approved.
                </p>
              ) : null}
            </section>
          ) : (
            <section className="card card-pad stack">
              <p className="muted">Only an ArchiveAdmin can approve or reject this.</p>
              {mine ? (
                <div className="row">
                  <button
                    type="button"
                    className="btn"
                    disabled={busy}
                    onClick={() =>
                      decide.withdraw.mutate(undefined, {
                        onSuccess: () => toast("Proposal withdrawn."),
                      })
                    }
                  >
                    Withdraw my proposal
                  </button>
                </div>
              ) : null}
            </section>
          )}
          {decideError ? (
            decideError instanceof ApiError && decideError.code === "self_approval" ? (
              <Callout tone="warn" title="You proposed this" role="alert">
                Another ArchiveAdmin has to approve it. That keeps every change to a second pair of
                eyes.
              </Callout>
            ) : (
              <ErrorCallout error={decideError} />
            )
          ) : null}
        </div>
        <div className="editor-preview" aria-label="Preview of the proposal">
          {p.preview ? (
            <PreviewPane preview={p.preview} />
          ) : (
            <p className="muted">No preview available.</p>
          )}
        </div>
      </div>
    </div>
  );
}

function onOpenText(onOpen: string, threshold: number | null): string {
  const label = (ON_OPEN_LABEL as Record<string, string | undefined>)[onOpen] ?? onOpen;
  return onOpen === "auto" && threshold ? `${label} (${threshold} min)` : label;
}
