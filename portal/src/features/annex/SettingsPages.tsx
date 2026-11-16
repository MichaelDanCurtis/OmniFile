import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useId, useState } from "react";
import { api, unwrap } from "../../api/client";
import { ErrorCallout, Loading } from "../../components/Feedback";
import { formatRelative } from "../../lib/format";
import {
  PROTOCOL_LABEL,
  VENDOR_LABEL,
  annexKeys,
  label,
  useAnnexDefaults,
  useFilers,
  type AnnexDefaults,
} from "./api";
import { AddFiler } from "./NewAnnexPage";
import { FilerFlows, edgeOnline } from "./FilerFlow";
import { useEdges } from "../../api/queries";

const CREDENTIALS: Record<string, string> = {
  machine_account: "AD computer account (Kerberos)",
  service_keytab: "Service account (Kerberos keytab)",
  sec_sys: "Address only (NFS sec=sys, host-trusted)",
};

/** Settings, Filers: the registered NAS filers, how the edge signs in, and health. */
export function FilersPage() {
  const filers = useFilers();
  const edges = useEdges();
  const [adding, setAdding] = useState(false);
  const list = filers.data?.items;
  const noEdge = edges.data !== undefined && !edges.data.edges.some(edgeOnline);
  return (
    <section className="stack" aria-labelledby="filers-h">
      <div className="row">
        <h2 id="filers-h">Filers</h2>
        <span className="spacer" />
        <button
          type="button"
          className="btn"
          disabled={noEdge}
          aria-describedby={noEdge ? "filers-need-edge" : undefined}
          onClick={() => setAdding(!adding)}
        >
          {adding ? "Close" : "Add a filer"}
        </button>
      </div>
      <p className="muted">
        NAS filers the edges read for annexes. Every NAS, ONTAP and PowerScale included, is read as
        an NFS or SMB server; nothing is written to it.
      </p>
      {noEdge ? (
        <p id="filers-need-edge" className="sr-only">
          Deploy an edge first: a filer is read by an edge.
        </p>
      ) : null}
      {adding ? <AddFiler onAdded={() => setAdding(false)} /> : null}
      {list ? <FilerFlows filers={list} /> : null}
      {filers.isError ? <ErrorCallout error={filers.error} /> : null}
      {!list ? (
        filers.isError ? null : (
          <Loading />
        )
      ) : list.length === 0 ? (
        <p className="empty">No filers yet.</p>
      ) : (
        <div className="table-wrap card">
          <table className="table" aria-label="Filers">
            <thead>
              <tr>
                <th scope="col">Name</th>
                <th scope="col">Where</th>
                <th scope="col">Kind</th>
                <th scope="col">Signs in as</th>
                <th scope="col">Changes seen by</th>
                <th scope="col">Health</th>
              </tr>
            </thead>
            <tbody>
              {list.map((f) => {
                const h = f.health as { ok?: boolean; error?: string | null } | null;
                return (
                  <tr key={f.filer_id}>
                    <th scope="row">{f.name}</th>
                    <td className="mono">
                      {label(PROTOCOL_LABEL, f.protocol)} {f.host}
                      {f.protocol === "smb" ? `\\${f.export}` : `:${f.export}`}
                    </td>
                    <td>{label(VENDOR_LABEL, f.vendor)}</td>
                    <td>{label(CREDENTIALS, f.credential_mode)}</td>
                    <td>
                      {f.event_source === "smb_notify" ? "Scans and SMB change notify" : "Scans"}
                    </td>
                    <td>
                      {h === null ? (
                        <span className="badge badge-outline">Not checked yet</span>
                      ) : h.ok ? (
                        <span className="badge badge-ok">Mounted</span>
                      ) : (
                        <span className="badge badge-danger">{h.error ?? "Not readable"}</span>
                      )}
                      {f.health_checked_at ? (
                        <span className="muted micro"> {formatRelative(f.health_checked_at)}</span>
                      ) : null}
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}
    </section>
  );
}

const FIELDS = [
  ["quiet_minutes", "Quiet period before a file is copied (minutes)", 1, 1440],
  ["quick_scan_minutes", "Quick scan every (minutes)", 1, 10080],
  ["full_scan_minutes", "Full scan every (minutes)", 5, 43200],
  ["reconcile_days", "Reconciliation every (days)", 1, 90],
] as const;

/** Settings, Annex defaults: what new annexes start with. */
export function AnnexDefaultsPage() {
  const defaults = useAnnexDefaults();
  const client = useQueryClient();
  const [edit, setEdit] = useState<Partial<AnnexDefaults>>({});
  const ids = { a: useId(), b: useId(), c: useId(), d: useId() };
  const save = useMutation({
    mutationFn: async (body: Partial<AnnexDefaults>) => {
      const current = await api.GET("/v1/settings/annex-defaults");
      return unwrap(
        api.PUT("/v1/settings/annex-defaults", {
          params: { header: { "If-Match": current.response.headers.get("ETag") ?? "" } },
          body,
        }),
      );
    },
    onSuccess: () => {
      setEdit({});
      void client.invalidateQueries({ queryKey: annexKeys.defaults });
    },
  });
  const d = defaults.data;
  if (!d) return defaults.isError ? <ErrorCallout error={defaults.error} /> : <Loading />;
  const value = (k: (typeof FIELDS)[number][0]) => edit[k] ?? d[k];
  const bad =
    FIELDS.some(([k, , lo, hi]) => value(k) < lo || value(k) > hi) ||
    value("full_scan_minutes") < value("quick_scan_minutes");
  return (
    <section className="stack" aria-labelledby="annex-defaults-h">
      <h2 id="annex-defaults-h">Annex defaults</h2>
      <p className="muted">
        What a new annex starts with. Each annex can change them in its own settings. A file is
        copied once its size and times have not changed for the quiet period; quick scans read only
        folders that changed, full scans every file.
      </p>
      <div className="grid-2">
        {FIELDS.map(([k, text, lo, hi], i) => {
          const id = [ids.a, ids.b, ids.c, ids.d][i];
          return (
            <div className="field" key={k}>
              <label className="field-label" htmlFor={id}>
                {text}
              </label>
              <input
                id={id}
                type="number"
                min={lo}
                max={hi}
                value={value(k)}
                onChange={(e) => setEdit((x) => ({ ...x, [k]: Number(e.target.value) }))}
              />
            </div>
          );
        })}
      </div>
      {bad ? (
        <p className="field-error">
          Each value within its range, and full scans no more often than quick ones.
        </p>
      ) : null}
      {save.isError ? <ErrorCallout error={save.error} /> : null}
      <div>
        <button
          type="button"
          className="btn btn-primary"
          disabled={bad || Object.keys(edit).length === 0 || save.isPending}
          onClick={() =>
            save.mutate({
              quiet_minutes: value("quiet_minutes"),
              quick_scan_minutes: value("quick_scan_minutes"),
              full_scan_minutes: value("full_scan_minutes"),
              reconcile_days: value("reconcile_days"),
            })
          }
        >
          Save defaults
        </button>
      </div>
    </section>
  );
}
