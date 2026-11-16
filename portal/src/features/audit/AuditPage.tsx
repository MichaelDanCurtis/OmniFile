import { useId, useState } from "react";
import { useAudit, useShares, type AuditFilters } from "../../api/queries";
import { useSession } from "../../app/session";
import { usePageTitle } from "../../app/usePageTitle";
import { Empty, ErrorCallout, Loading } from "../../components/Feedback";
import { formatDateTime, fromLocalInput } from "../../lib/format";
import { isArchiveAdmin } from "../../lib/permissions";

const DECISION_TONE = { allow: "badge-ok", deny: "badge-danger", info: "badge-info" } as const;

export function AuditPage() {
  usePageTitle("Audit");
  const { me } = useSession();
  const admin = isArchiveAdmin(me);
  const shares = useShares();
  const owned = (shares.data?.items ?? []).filter((s) => admin || s.my_access.owner);
  const [draft, setDraft] = useState({ share_id: "", action: "", actor: "", from: "", to: "" });
  const [filters, setFilters] = useState<AuditFilters>({});
  const effective: AuditFilters = admin
    ? filters
    : { ...filters, share_id: filters.share_id ?? owned[0]?.share_id };
  const audit = useAudit(effective, admin || !!effective.share_id);
  const rows = audit.data?.pages.flatMap((p) => p.items) ?? [];
  const shareName = (id: string | null | undefined) =>
    id ? (shares.data?.items.find((s) => s.share_id === id)?.name ?? `share ${id}`) : "—";
  const ids = { share: useId(), action: useId(), actor: useId(), from: useId(), to: useId() };

  return (
    <div className="page">
      <div className="page-head">
        <div>
          <h1>Audit</h1>
          <p className="page-question">Who did what, and when?</p>
          <p className="lede">
            Append-only, with the AD identity behind every entry.
            {admin ? "" : " You see the shares you own."}
          </p>
        </div>
      </div>
      <form
        className="toolbar card card-pad"
        onSubmit={(e) => {
          e.preventDefault();
          setFilters({
            ...(draft.share_id ? { share_id: draft.share_id } : {}),
            ...(draft.action ? { action: draft.action } : {}),
            ...(draft.actor ? { actor: draft.actor } : {}),
            ...(fromLocalInput(draft.from)
              ? { from: fromLocalInput(draft.from) ?? undefined }
              : {}),
            ...(fromLocalInput(draft.to) ? { to: fromLocalInput(draft.to) ?? undefined } : {}),
          });
        }}
      >
        <div className="field">
          <label className="field-label" htmlFor={ids.share}>
            Share
          </label>
          <select
            id={ids.share}
            value={draft.share_id}
            onChange={(e) => setDraft((d) => ({ ...d, share_id: e.target.value }))}
          >
            {admin ? <option value="">All shares</option> : null}
            {owned.map((s) => (
              <option key={s.share_id} value={s.share_id}>
                {s.name}
              </option>
            ))}
          </select>
        </div>
        <div className="field">
          <label className="field-label" htmlFor={ids.action}>
            Action starts with
          </label>
          <input
            id={ids.action}
            type="text"
            placeholder="recall, pin, worm…"
            value={draft.action}
            onChange={(e) => setDraft((d) => ({ ...d, action: e.target.value }))}
          />
        </div>
        <div className="field">
          <label className="field-label" htmlFor={ids.actor}>
            Actor
          </label>
          <input
            id={ids.actor}
            type="text"
            className="mono"
            placeholder="user:123, app:…"
            value={draft.actor}
            onChange={(e) => setDraft((d) => ({ ...d, actor: e.target.value }))}
          />
        </div>
        <div className="field">
          <label className="field-label" htmlFor={ids.from}>
            From
          </label>
          <input
            id={ids.from}
            type="datetime-local"
            value={draft.from}
            onChange={(e) => setDraft((d) => ({ ...d, from: e.target.value }))}
          />
        </div>
        <div className="field">
          <label className="field-label" htmlFor={ids.to}>
            To
          </label>
          <input
            id={ids.to}
            type="datetime-local"
            value={draft.to}
            onChange={(e) => setDraft((d) => ({ ...d, to: e.target.value }))}
          />
        </div>
        <button type="submit" className="btn btn-primary" style={{ alignSelf: "flex-end" }}>
          Apply
        </button>
      </form>
      {!admin && owned.length === 0 ? (
        <Empty title="Nothing to show">
          <p>You don't own any share.</p>
        </Empty>
      ) : audit.isPending ? (
        <Loading />
      ) : audit.isError ? (
        <ErrorCallout error={audit.error} />
      ) : rows.length === 0 ? (
        <div className="table-wrap">
          <Empty title="No matching records" />
        </div>
      ) : (
        <div className="table-wrap">
          <table className="table" aria-label="Audit records">
            <thead>
              <tr>
                <th scope="col">When</th>
                <th scope="col">Who</th>
                <th scope="col">Action</th>
                <th scope="col">Decision</th>
                <th scope="col">Where</th>
                {admin ? <th scope="col">Client</th> : null}
                <th scope="col">Details</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((a) => (
                <tr key={a.audit_id}>
                  <td className="num">{formatDateTime(a.at)}</td>
                  <td>
                    <span className="stack" style={{ gap: 0 }}>
                      <span>{a.actor_display_name ?? a.actor}</span>
                      <span className="mono muted">
                        {a.actor}
                        {a.proof === "claimed" ? " · claimed" : ""}
                      </span>
                    </span>
                  </td>
                  <td className="mono">{a.action}</td>
                  <td>
                    <span className={`badge ${DECISION_TONE[a.decision]}`}>{a.decision}</span>
                  </td>
                  <td>
                    {shareName(a.share_id)}
                    {a.file_id ? <span className="mono muted"> · file {a.file_id}</span> : null}
                  </td>
                  {admin ? <td className="mono">{a.client_addr ?? "—"}</td> : null}
                  <td>
                    {a.details && Object.keys(a.details).length > 0 ? (
                      <details>
                        <summary>Show</summary>
                        <pre className="json">{JSON.stringify(a.details, null, 2)}</pre>
                      </details>
                    ) : (
                      <span className="muted">—</span>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
          {audit.hasNextPage ? (
            <div className="table-foot">
              <button
                type="button"
                className="btn btn-sm"
                disabled={audit.isFetchingNextPage}
                onClick={() => void audit.fetchNextPage()}
              >
                {audit.isFetchingNextPage ? "Loading…" : "Load more"}
              </button>
            </div>
          ) : null}
        </div>
      )}
    </div>
  );
}
