import { useState } from "react";
import { Link } from "react-router";
import { useEdges } from "../../api/queries";
import type { components } from "../../api/schema.gen";
import { useSession } from "../../app/session";
import { usePageTitle } from "../../app/usePageTitle";
import { ErrorCallout, Loading } from "../../components/Feedback";
import { formatBytes, formatDateTime, formatRelative } from "../../lib/format";
import { isArchiveAdmin } from "../../lib/permissions";
import { NewCodeDialog } from "./AddEdgeDialog";
import { Tip } from "../../components/Tip";
import { RevokeEdgeDialog } from "./RevokeEdgeDialog";
import { HeroMetric } from "../../components/PageHead";

type Edge = components["schemas"]["Edge"];

/** status and guard_state are open enums: an unknown value is shown as is. */
const STATUS: Record<string, { label: string; badge: string }> = {
  ok: { label: "OK", badge: "badge-ok" },
  attention: { label: "Needs attention", badge: "badge-warn" },
  silent: { label: "Not reporting", badge: "badge-danger" },
  pending: { label: "Not enrolled", badge: "badge-outline" },
};

const GUARD: Record<string, string> = {
  armed: "Armed",
  degraded: "Degraded",
  down: "Down",
};

function why(e: Edge): string | null {
  if (e.status === "silent") {
    return e.last_heartbeat_at ? "No heartbeat for 15 minutes" : "Never sent a heartbeat";
  }
  if (e.status !== "attention") return null;
  const reasons = [];
  if (e.guard_state && e.guard_state !== "armed") reasons.push("SMB guard not armed");
  const free = e.health.data_free_bytes;
  const cap = e.health.data_capacity_bytes;
  if (free !== undefined && cap) {
    if (free / cap < 0.1) reasons.push("data volume almost full");
  }
  if (e.health.recovery_used === 1) reasons.push("break-glass account used: seal it again");
  if ((e.health.quiet_stale_s ?? 0) >= 300)
    reasons.push("can't read its shares' quiet periods: uploads wait the longest period");
  return reasons.join("; ") || null;
}

function space(e: Edge): string {
  const free = e.health.data_free_bytes;
  const cap = e.health.data_capacity_bytes;
  if (free === undefined || !cap) return "—";
  return `${formatBytes(free)} free of ${formatBytes(cap)}`;
}

export function EdgesPage() {
  usePageTitle("Edges");
  const { me } = useSession();
  const admin = isArchiveAdmin(me);
  const edges = useEdges(admin);
  const [recode, setRecode] = useState<Edge | null>(null);
  const [revoking, setRevoking] = useState<Edge | null>(null);

  if (!admin) {
    return (
      <div className="page">
        <h1>Edges</h1>
        <ErrorCallout error={new Error("Edges are for ArchiveAdmin.")} />
      </div>
    );
  }
  const list = edges.data?.edges;
  const counts = (list ?? []).reduce<Record<string, number>>((n, e) => {
    n[e.status] = (n[e.status] ?? 0) + 1;
    return n;
  }, {});
  return (
    <div className="page">
      <div className="page-head">
        <div>
          <h1>Edges</h1>
          <p className="page-question">Is every edge filer healthy and reporting?</p>
          <p className="lede">
            Each edge reports every minute; the same checks raise Azure Monitor alerts.
          </p>
        </div>
        <div className="page-head-aside">
          <div className="actions">
            <Link className="btn btn-primary" to="/edges/new">
              Add edge
            </Link>
          </div>
          {list && list.length > 0 ? (
            <HeroMetric
              label="Reporting OK"
              value={String(counts.ok ?? 0)}
              unit={`/ ${list.length}`}
              tone={
                (counts.silent ?? 0) > 0
                  ? "signal"
                  : (counts.ok ?? 0) === list.length
                    ? "ok"
                    : "warn"
              }
              sub="edges, from their last heartbeat"
            />
          ) : null}
        </div>
      </div>
      <NewCodeDialog edge={recode} onClose={() => setRecode(null)} />
      <RevokeEdgeDialog edge={revoking} onClose={() => setRevoking(null)} />
      {edges.isError ? <ErrorCallout error={edges.error} /> : null}
      {!list ? (
        edges.isError ? null : (
          <Loading />
        )
      ) : list.length === 0 ? (
        <p className="card card-pad">
          No edges yet. <Link to="/edges/new">Add one</Link>: the steps take you from the hypervisor
          to its first heartbeat.
        </p>
      ) : (
        <>
          <p className="card card-pad" aria-live="polite">
            {list.length} {list.length === 1 ? "edge" : "edges"}:{" "}
            {Object.entries(counts)
              .map(([s, n]) => `${n} ${(STATUS[s]?.label ?? s).toLowerCase()}`)
              .join(", ")}
            .
          </p>
          <div className="table-wrap card">
            <table className="table table-dense" aria-label="Edges">
              <thead>
                <tr>
                  <th scope="col">Edge</th>
                  <th scope="col">Status</th>
                  <th scope="col">Last heartbeat</th>
                  <th scope="col">SMB guard</th>
                  <th scope="col">Data volume</th>
                  <th scope="col" className="col-num">
                    Outbox
                  </th>
                  <th scope="col" className="col-num">
                    Recalls waiting
                  </th>
                  <th scope="col">Version</th>
                </tr>
              </thead>
              <tbody>
                {list.map((e) => {
                  const s = STATUS[e.status] ?? { label: e.status, badge: "badge-info" };
                  const reason = why(e);
                  return (
                    <tr key={e.edge_id}>
                      <th scope="row" className="nowrap">
                        {e.name}
                      </th>
                      <td>
                        <div className="row">
                          <span className={`badge ${s.badge}`}>{s.label}</span>
                          {e.state === "pending" ? (
                            <button
                              type="button"
                              className="btn btn-link"
                              onClick={() => setRecode(e)}
                            >
                              New code
                            </button>
                          ) : null}
                          <button
                            type="button"
                            className="btn btn-link"
                            aria-label={`Revoke ${e.name}`}
                            onClick={() => setRevoking(e)}
                          >
                            Revoke…
                          </button>
                        </div>
                        {reason ? <div className="muted cell-line">{reason}</div> : null}
                      </td>
                      <td className="nowrap">
                        {e.last_heartbeat_at ? (
                          <Tip label="Exact time" tip={formatDateTime(e.last_heartbeat_at)}>
                            <time dateTime={e.last_heartbeat_at}>
                              {formatRelative(e.last_heartbeat_at)}
                            </time>
                          </Tip>
                        ) : (
                          "Never"
                        )}
                      </td>
                      <td>{e.guard_state ? (GUARD[e.guard_state] ?? e.guard_state) : "—"}</td>
                      <td className="nowrap">{space(e)}</td>
                      <td className="col-num">{e.health.outbox_depth ?? "—"}</td>
                      <td className="col-num">{e.health.recalls_waiting ?? "—"}</td>
                      <td className="nowrap">
                        {e.software_version ?? "—"}
                        {e.kernel ? <span className="muted">, kernel {e.kernel}</span> : null}
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        </>
      )}
    </div>
  );
}
