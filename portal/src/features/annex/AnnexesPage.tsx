import { Link } from "react-router";
import { useSession } from "../../app/session";
import { usePageTitle } from "../../app/usePageTitle";
import { ErrorCallout, Loading } from "../../components/Feedback";
import { formatRelative } from "../../lib/format";
import { isArchiveAdmin, isDataSteward } from "../../lib/permissions";
import { PROTOCOL_LABEL, STATE_LABEL, label, useAnnexes } from "./api";

/** Annexes: NAS directories copied to Azure (annex.md, phase A1). */
export function AnnexesPage() {
  usePageTitle("Annexes");
  const { me } = useSession();
  const annexes = useAnnexes();
  const canCreate = isArchiveAdmin(me) || isDataSteward(me);
  const list = annexes.data?.items;
  return (
    <div className="page">
      <div className="page-head">
        <div>
          <h1>Annexes</h1>
          <p className="page-question">
            Which NAS folders are copied to Azure, and how are they doing?
          </p>
          <p className="lede">
            People keep using the NAS as they do today; nothing on it is changed.
          </p>
        </div>
        {canCreate ? (
          <Link className="btn btn-primary" to="/annexes/new">
            Create an Annex
          </Link>
        ) : null}
      </div>
      {annexes.isError ? <ErrorCallout error={annexes.error} /> : null}
      {!list ? (
        annexes.isError ? null : (
          <Loading />
        )
      ) : list.length === 0 ? (
        <p className="empty">No annexes yet.</p>
      ) : (
        <div className="table-wrap card">
          <table className="table" aria-label="Annexes">
            <thead>
              <tr>
                <th scope="col">Name</th>
                <th scope="col">Folder</th>
                <th scope="col">Mode</th>
                <th scope="col">State</th>
                <th scope="col">Last full scan</th>
              </tr>
            </thead>
            <tbody>
              {list.map((a) => (
                <tr key={a.annex_id}>
                  <th scope="row">
                    <Link to={`/annexes/${a.annex_id}`}>{a.name}</Link>
                  </th>
                  <td className="mono">
                    {a.unc}
                    {a.host_trusted ? (
                      <span className="badge badge-warn" title="NFS sec=sys">
                        Host-trusted
                      </span>
                    ) : null}
                  </td>
                  <td>{a.mode === "copy" ? "Copy" : a.mode}</td>
                  <td>{label(STATE_LABEL, a.state)}</td>
                  <td>{a.last_full_scan_at ? formatRelative(a.last_full_scan_at) : "Not yet"}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      <p className="muted micro">
        Protocols: {Object.values(PROTOCOL_LABEL).join(", ")}. NetApp ONTAP and Dell PowerScale are
        annexed as NFS or SMB servers.
      </p>
    </div>
  );
}
