import { useState } from "react";
import { Link } from "react-router";
import { useShares } from "../../api/queries";
import { useSession } from "../../app/session";
import { usePageTitle } from "../../app/usePageTitle";
import { Empty, ErrorCallout, Loading } from "../../components/Feedback";
import { isArchiveAdmin } from "../../lib/permissions";
import { shareRoleLabel } from "../files/FilesIndexPage";
import { AddShareDialog } from "./AddShareDialog";

export const ON_OPEN_LABEL = {
  fail: "Tell them straight away",
  wait: "Program waits",
  auto: "Wait only if quick",
} as const;

/** A share with an error check needs an answer: for example a sec=sys share whose
 *  folders were restricted after it was made (decision 39). The checks say what. */
function AttentionBadge({ checks }: { checks: { severity: string; message: string }[] }) {
  const errors = checks.filter((c) => c.severity === "error");
  if (errors.length === 0) return null;
  return (
    <span className="badge badge-warn" title={errors.map((c) => c.message).join("\n")}>
      Needs attention
    </span>
  );
}

export function SharesPage() {
  usePageTitle("Shares");
  const { me } = useSession();
  const shares = useShares();
  const admin = isArchiveAdmin(me);
  const [adding, setAdding] = useState(false);
  const manageable = (shares.data?.items ?? []).filter((s) => admin || s.my_access.owner);
  return (
    <div className="page">
      <div className="page-head">
        <div>
          <h1>Shares</h1>
          <p className="page-question">
            How does each share behave when a file is archived or changed?
          </p>
          <p className="lede">Rewind, NFS security, WORM and access, for the shares you manage.</p>
        </div>
        {admin ? (
          <button type="button" className="btn btn-primary" onClick={() => setAdding(true)}>
            Add share
          </button>
        ) : null}
      </div>
      {admin ? <AddShareDialog open={adding} onClose={() => setAdding(false)} /> : null}
      {shares.isPending ? (
        <Loading />
      ) : shares.isError ? (
        <ErrorCallout error={shares.error} />
      ) : manageable.length === 0 ? (
        <Empty title="No shares to manage">
          <p>
            {admin
              ? "No shares yet. Add one to serve a dataset."
              : "You're not an Owner of any share."}
          </p>
        </Empty>
      ) : (
        <div className="table-wrap">
          <table className="table" aria-label="Shares">
            <thead>
              <tr>
                <th scope="col">Share</th>
                <th scope="col">Dataset</th>
                <th scope="col">Archived files</th>
                <th scope="col" className="col-num">
                  Rewind
                </th>
                <th scope="col">Security</th>
                <th scope="col">WORM</th>
                <th scope="col">You</th>
              </tr>
            </thead>
            <tbody>
              {manageable.map((s) => (
                <tr key={s.share_id}>
                  <td>
                    <span className="cell-name">
                      <Link to={`/shares/${s.share_id}`}>{s.name}</Link>
                    </span>
                    <AttentionBadge checks={s.checks} />
                  </td>
                  <td>{s.dataset_name}</td>
                  <td>
                    {ON_OPEN_LABEL[s.on_open]}
                    {s.on_open === "auto" && s.wait_threshold_minutes
                      ? ` (${s.wait_threshold_minutes} min)`
                      : ""}
                  </td>
                  <td className="col-num num">{s.rewind.depth_days} days</td>
                  <td>
                    <span className="row" style={{ gap: 4 }}>
                      {s.nfs_sec.map((sec) => (
                        <span key={sec} className="badge badge-outline mono">
                          {sec}
                        </span>
                      ))}
                      {s.trust_level === "host_trusted" ? (
                        <span className="badge badge-warn">Host-trusted</span>
                      ) : null}
                    </span>
                  </td>
                  <td>
                    {s.worm.enabled ? (
                      <span className="badge badge-info">
                        {s.worm.mode === "locked" ? "Locked" : "Testing"}
                      </span>
                    ) : (
                      <span className="muted">Off</span>
                    )}
                  </td>
                  <td>{shareRoleLabel(s)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}
