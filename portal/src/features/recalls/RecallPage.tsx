import { Link, useNavigate, useParams } from "react-router";
import { useRecall, useRequestRecall } from "../../api/queries";
import { usePageTitle } from "../../app/usePageTitle";
import { Callout, ErrorCallout, Loading } from "../../components/Feedback";
import { RecallSteps } from "../../components/RecallSteps";
import { useToast } from "../../components/Toasts";
import {
  ETA_CONFIDENCE_LABEL,
  formatDateTime,
  formatRelative,
  pluralize,
  priorityLabel,
} from "../../lib/format";
import { RecallBadge } from "./RecallsPage";

/** The four-step view of one recall; polls every 5 seconds until it ends. */
export function RecallPage() {
  const { recallId } = useParams();
  const recall = useRecall(recallId);
  const again = useRequestRecall();
  const navigate = useNavigate();
  const toast = useToast();
  usePageTitle(recall.data?.name ? `Recall of ${recall.data.name}` : "Recall");

  if (recall.isPending) return <Loading />;
  if (recall.isError) {
    return (
      <div className="page">
        <ErrorCallout error={recall.error} />
      </div>
    );
  }
  const r = recall.data;
  const parent = r.parent_id;
  const others = Math.max(0, (r.requesters?.count ?? 1) - (r.requesters?.me ? 1 : 0));

  return (
    <div className="page">
      <nav aria-label="Breadcrumb">
        <ol className="crumbs">
          <li>
            <Link to="/recalls">Recalls</Link>
          </li>
          <li>
            <span aria-current="page">{r.name ?? "File"}</span>
          </li>
        </ol>
      </nav>
      <div className="page-head">
        <div>
          <h1>{r.name ?? "A file you can no longer read"}</h1>
          <p className="page-question">When will this file be back?</p>
          <p className="lede row">
            <RecallBadge state={r.state} reason={r.superseded?.reason} />
            <span>
              {priorityLabel(r.priority)} · requested {formatRelative(r.requested_at)}
            </span>
          </p>
        </div>
        {parent && r.share_id ? (
          <div className="actions">
            <Link className="btn" to={`/files/${r.share_id}/${parent}?file=${r.file_id}`}>
              Show in folder
            </Link>
          </div>
        ) : null}
      </div>

      <div className="card card-pad stack-lg">
        <RecallSteps recall={r} />
        {r.state === "available" ? (
          <Callout tone="ok" title="It's back">
            Open {r.name ?? "the file"} from the share as usual. It stays on the gateway while
            people use it.
          </Callout>
        ) : r.state === "failed" ? (
          <Callout tone="danger" title="The recall didn't finish" role="alert">
            <p>{r.failure ?? "The storage provider couldn't finish the restore."}</p>
            <button
              type="button"
              className="btn btn-sm"
              style={{ marginTop: 8 }}
              disabled={again.isPending}
              onClick={() =>
                again.mutate(
                  {
                    file_id: r.file_id,
                    version_id: r.version_id,
                    priority: r.priority,
                  },
                  {
                    onSuccess: (next) => {
                      toast("Recall started again.");
                      void navigate(`/recalls/${next.recall_id}`);
                    },
                  },
                )
              }
            >
              Recall it again
            </button>
            {again.isError ? <ErrorCallout error={again.error} /> : null}
          </Callout>
        ) : r.state === "superseded" ? (
          // Not a failure, and nothing to try again: the file itself changed.
          <Callout title="The recall stopped">
            <p>
              {r.superseded?.text ?? "The file changed while it was being recalled."}
              {r.superseded?.by
                ? ` ${r.superseded.reason === "deleted" ? "Deleted" : "Replaced"} by ${r.superseded.by}.`
                : ""}
            </p>
            <p>
              {r.superseded?.reason === "deleted"
                ? "Nothing failed. The file is no longer on the share."
                : "Nothing failed. The file on the share is the new one."}
            </p>
          </Callout>
        ) : null}
        <dl className="kv">
          <dt>Expected</dt>
          <dd>
            {r.state === "available"
              ? `Ready since ${formatDateTime(r.completed_at)}`
              : r.state === "superseded"
                ? `Stopped ${formatDateTime(r.completed_at)}`
                : r.eta
                  ? `${formatDateTime(r.eta)} (${formatRelative(r.eta)}, ${ETA_CONFIDENCE_LABEL[r.eta_confidence ?? "none"]})`
                  : "No time promised. Above 10 GB the provider gives no time, so neither do we."}
          </dd>
          {r.due_by ? (
            <>
              <dt>Needed by</dt>
              <dd>{formatDateTime(r.due_by)}</dd>
            </>
          ) : null}
          <dt>Waiting</dt>
          <dd>
            {r.requesters?.me
              ? others > 0
                ? `You and ${pluralize(others, "other person", "other people")}`
                : "You"
              : pluralize(r.requesters?.count ?? 1, "person", "people")}
            . Everyone waiting gets a Teams message at each step.
          </dd>
        </dl>
      </div>
    </div>
  );
}
