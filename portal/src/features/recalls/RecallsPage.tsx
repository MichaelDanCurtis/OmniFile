import { useId, useState } from "react";
import { Link } from "react-router";
import { useRecalls, useShares } from "../../api/queries";
import type { RecallState } from "../../api/types";
import { useSession } from "../../app/session";
import { usePageTitle } from "../../app/usePageTitle";
import { Empty, ErrorCallout, Loading } from "../../components/Feedback";
import {
  ETA_CONFIDENCE_LABEL,
  RECALL_STATE_LABEL,
  formatDateTime,
  formatRelative,
  priorityLabel,
  supersededLabel,
} from "../../lib/format";
import { isArchiveAdmin } from "../../lib/permissions";
import { HeroMetric } from "../../components/PageHead";

/** `reason`: a superseded recall's (`replaced` or `deleted`), so the badge says which. */
export function RecallBadge({ state, reason }: { state: RecallState; reason?: string | null }) {
  const tone =
    state === "available"
      ? "badge-ok"
      : state === "failed"
        ? "badge-danger"
        : state === "cancelled" || state === "superseded"
          ? ""
          : "badge-accent";
  return (
    <span className={`badge ${tone}`}>
      {state === "superseded" ? supersededLabel(reason) : RECALL_STATE_LABEL[state]}
    </span>
  );
}

const ENDED: RecallState[] = ["available", "failed", "cancelled", "superseded"];

type Scope = "mine" | "share" | "all";

export function RecallsPage() {
  usePageTitle("Recalls");
  const { me } = useSession();
  const shares = useShares();
  const ownedShares = (shares.data?.items ?? []).filter(
    (s) => s.my_access.owner || isArchiveAdmin(me),
  );
  const [scope, setScope] = useState<Scope>("mine");
  const [shareId, setShareId] = useState<string>("");
  const [state, setState] = useState<RecallState | "">("");
  const effectiveShare = scope === "share" ? shareId || ownedShares[0]?.share_id : undefined;
  const recalls = useRecalls(scope, effectiveShare, state || undefined);
  const shareSel = useId();
  const stateSel = useId();

  return (
    <div className="page">
      <div className="page-head">
        <div>
          <h1>Recalls</h1>
          <p className="page-question">
            Which files are coming back from Archive, and how far along?
          </p>
          <p className="lede">Four steps each: requested, rehydrating, downloading, available.</p>
        </div>
        {recalls.data ? (
          <HeroMetric
            label="Under way"
            value={String(recalls.data.items.filter((r) => !ENDED.includes(r.state)).length)}
            sub="of the recalls listed here"
          />
        ) : null}
      </div>
      <div className="toolbar">
        <div className="seg" role="group" aria-label="Whose recalls">
          <button type="button" aria-pressed={scope === "mine"} onClick={() => setScope("mine")}>
            Mine
          </button>
          {ownedShares.length > 0 ? (
            <button
              type="button"
              aria-pressed={scope === "share"}
              onClick={() => setScope("share")}
            >
              A share I manage
            </button>
          ) : null}
          {isArchiveAdmin(me) ? (
            <button type="button" aria-pressed={scope === "all"} onClick={() => setScope("all")}>
              Everyone's
            </button>
          ) : null}
        </div>
        {scope === "share" ? (
          <>
            <label htmlFor={shareSel} className="sr-only">
              Share
            </label>
            <select
              id={shareSel}
              value={effectiveShare ?? ""}
              onChange={(e) => setShareId(e.target.value)}
            >
              {ownedShares.map((s) => (
                <option key={s.share_id} value={s.share_id}>
                  {s.name}
                </option>
              ))}
            </select>
          </>
        ) : null}
        <label htmlFor={stateSel} className="sr-only">
          State
        </label>
        <select
          id={stateSel}
          value={state}
          onChange={(e) => setState(e.target.value as RecallState | "")}
        >
          <option value="">Any state</option>
          <option value="requested">Requested</option>
          <option value="rehydrating">Rehydrating</option>
          <option value="downloading">Downloading</option>
          <option value="available">Available</option>
          <option value="failed">Failed</option>
          <option value="superseded">Stopped: file replaced or deleted</option>
        </select>
      </div>
      {recalls.isPending ? (
        <Loading />
      ) : recalls.isError ? (
        <ErrorCallout error={recalls.error} />
      ) : recalls.data.items.length === 0 ? (
        <div className="table-wrap">
          <Empty title="No recalls">
            <p>
              Recall an archived file from <Link to="/files">Files</Link>, or open it on the share.
            </p>
          </Empty>
        </div>
      ) : (
        <div className="table-wrap">
          <table className="table" aria-label="Recalls">
            <thead>
              <tr>
                <th scope="col">File</th>
                <th scope="col">Step</th>
                <th scope="col">Priority</th>
                <th scope="col">Requested</th>
                <th scope="col">Expected</th>
                <th scope="col" className="col-num">
                  Waiting
                </th>
              </tr>
            </thead>
            <tbody>
              {recalls.data.items.map((r) => (
                <tr key={r.recall_id}>
                  <td>
                    <span className="cell-name">
                      <Link to={`/recalls/${r.recall_id}`}>
                        {r.name ?? "A file you can no longer read"}
                      </Link>
                    </span>
                  </td>
                  <td>
                    <RecallBadge state={r.state} reason={r.superseded?.reason} />
                  </td>
                  <td>{priorityLabel(r.priority)}</td>
                  <td className="num">{formatRelative(r.requested_at)}</td>
                  <td className="num">
                    {r.state === "available"
                      ? `Ready ${formatRelative(r.completed_at)}`
                      : r.eta
                        ? `${formatDateTime(r.eta)} · ${ETA_CONFIDENCE_LABEL[r.eta_confidence ?? "none"]}`
                        : r.state === "failed" || r.state === "superseded"
                          ? "—"
                          : "No time promised"}
                  </td>
                  <td className="col-num num">{r.requesters?.count ?? 1}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}
