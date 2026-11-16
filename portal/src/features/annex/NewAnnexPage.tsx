import { useId, useMemo, useState } from "react";
import { Link, useNavigate, useSearchParams } from "react-router";
import { useAddDataset, useBackends, useDatasets, useEdges } from "../../api/queries";
import type { GroupRef } from "../../api/types";
import { useSession } from "../../app/session";
import { usePageTitle } from "../../app/usePageTitle";
import { ErrorCallout, Loading } from "../../components/Feedback";
import { GroupPicker, groupName } from "../../components/GroupPicker";
import { isArchiveAdmin, isDataSteward } from "../../lib/permissions";
import {
  PROTOCOL_LABEL,
  VENDOR_LABEL,
  type FilerCreate,
  label,
  useAddAnnex,
  useAddFiler,
  useAnnexDefaults,
  useFilers,
} from "./api";

const NAME = /^[A-Za-z0-9][A-Za-z0-9._-]{0,62}$/;

function Question({ n, title, children }: { n: number; title: string; children: React.ReactNode }) {
  const id = useId();
  return (
    <section className="question" aria-labelledby={id}>
      <span className="q-num" aria-hidden="true">
        {n}
      </span>
      <h2 id={id}>{title}</h2>
      <div className="question-body">{children}</div>
    </section>
  );
}

/** "Add a filer" inline (ArchiveAdmin): host, protocol, export, edge. */
export function AddFiler({ onAdded }: { onAdded: (filerId: string) => void }) {
  const { me } = useSession();
  const edges = useEdges(isArchiveAdmin(me));
  const add = useAddFiler();
  const ids = {
    name: useId(),
    host: useId(),
    proto: useId(),
    exp: useId(),
    edge: useId(),
    cred: useId(),
    vendor: useId(),
  };
  const [f, setF] = useState<FilerCreate>({
    name: "",
    host: "",
    protocol: "smb",
    export: "",
    edge_id: "",
    credential_mode: "machine_account",
    vendor: "generic",
  });
  const set = (k: keyof FilerCreate) => (e: { target: { value: string } }) =>
    setF((x) => ({ ...x, [k]: e.target.value }));
  const ok = NAME.test(f.name) && f.host && f.export && f.edge_id;
  return (
    <div className="card card-pad stack" aria-label="Add a filer">
      <div className="grid-2">
        <div className="field">
          <label className="field-label" htmlFor={ids.name}>
            Filer name
          </label>
          <input id={ids.name} value={f.name} onChange={set("name")} />
        </div>
        <div className="field">
          <label className="field-label" htmlFor={ids.host}>
            Host
          </label>
          <input
            id={ids.host}
            value={f.host}
            onChange={set("host")}
            placeholder="ontap01.corp.example"
          />
        </div>
        <div className="field">
          <label className="field-label" htmlFor={ids.proto}>
            Protocol
          </label>
          <select id={ids.proto} value={f.protocol} onChange={set("protocol")}>
            <option value="smb">SMB</option>
            <option value="nfs4">NFSv4</option>
            <option value="nfs3">NFSv3</option>
          </select>
        </div>
        <div className="field">
          <label className="field-label" htmlFor={ids.exp}>
            {f.protocol === "smb" ? "Share name" : "Export path"}
          </label>
          <input
            id={ids.exp}
            value={f.export}
            onChange={set("export")}
            placeholder={f.protocol === "smb" ? "geo" : "/export/geo"}
          />
        </div>
        <div className="field">
          <label className="field-label" htmlFor={ids.vendor}>
            Kind of NAS
          </label>
          <select id={ids.vendor} value={f.vendor} onChange={set("vendor")}>
            {Object.entries(VENDOR_LABEL).map(([v, l]) => (
              <option key={v} value={v}>
                {l}
              </option>
            ))}
          </select>
          <p className="field-hint">ONTAP and PowerScale are read as NFS or SMB servers.</p>
        </div>
        <div className="field">
          <label className="field-label" htmlFor={ids.edge}>
            Edge next to it
          </label>
          <select id={ids.edge} value={f.edge_id} onChange={set("edge_id")}>
            <option value="">Choose an edge</option>
            {(edges.data?.edges ?? []).map((e) => (
              <option key={e.edge_id} value={e.edge_id}>
                {e.name}
              </option>
            ))}
          </select>
        </div>
        <div className="field">
          <label className="field-label" htmlFor={ids.cred}>
            The edge signs in as
          </label>
          <select id={ids.cred} value={f.credential_mode} onChange={set("credential_mode")}>
            <option value="machine_account">Its AD computer account (Kerberos)</option>
            <option value="service_keytab">A service account (Kerberos keytab)</option>
            {f.protocol !== "smb" ? (
              <option value="sec_sys">Its address only (NFS sec=sys)</option>
            ) : null}
          </select>
          {f.credential_mode === "sec_sys" ? (
            <p className="field-hint">
              Host-trusted: the NAS trusts the edge's address, not a proven identity. Export it
              read-only to the edge's address alone.
            </p>
          ) : null}
        </div>
      </div>
      {add.isError ? <ErrorCallout error={add.error} /> : null}
      <div>
        <button
          type="button"
          className="btn"
          disabled={!ok || add.isPending}
          onClick={() =>
            add.mutate(
              { ...f, ...(f.protocol === "smb" ? {} : { event_source: "scan" as const }) },
              { onSuccess: (r) => onAdded(r.filer_id) },
            )
          }
        >
          Add filer
        </button>
      </div>
    </div>
  );
}

/** Create an Annex: steps 1, 2, 4, 5 and 7 of annex.md 8.1. */
export function NewAnnexPage() {
  usePageTitle("Create an Annex");
  const { me } = useSession();
  const admin = isArchiveAdmin(me);
  const allowed = admin || isDataSteward(me);
  const [params] = useSearchParams();
  const navigate = useNavigate();
  const filers = useFilers(allowed);
  const datasets = useDatasets(allowed);
  const backends = useBackends(allowed);
  const defaults = useAnnexDefaults(allowed);
  const addDataset = useAddDataset();
  const addAnnex = useAddAnnex();
  const ids = {
    filer: useId(),
    path: useId(),
    name: useId(),
    dataset: useId(),
    dsName: useId(),
    backend: useId(),
    quiet: useId(),
    quick: useId(),
    full: useId(),
  };
  const [filerId, setFilerId] = useState(params.get("filer") ?? "");
  const [addingFiler, setAddingFiler] = useState(false);
  const [path, setPath] = useState(params.get("path") ?? "");
  const [name, setName] = useState("");
  const [datasetId, setDatasetId] = useState("");
  const [newDataset, setNewDataset] = useState({ name: "", backend_id: "" });
  const [owners, setOwners] = useState<GroupRef | null>(null);
  const [more, setMore] = useState(false);
  const [mode, setMode] = useState<"copy" | "archive_after" | "move_now" | "custom">("copy");
  const [days, setDays] = useState(30);
  const daysId = useId();
  const [settings, setSettings] = useState<Record<string, number>>({});
  const filer = (filers.data?.items ?? []).find((f) => f.filer_id === filerId);
  const usable = useMemo(
    () =>
      (datasets.data?.items ?? []).filter(
        (d) =>
          filer && d.edge_id === filer.edge_id && d.shares.length === 0 && d.backend_kind !== "s3",
      ),
    [datasets.data, filer],
  );
  const homes = (backends.data?.backends ?? []).filter((b) => b.can_be_home);
  const creating = datasetId === "new";
  const datasetOk = creating ? NAME.test(newDataset.name) && newDataset.backend_id : datasetId;
  const ready = filer && NAME.test(name) && datasetOk && owners && !addAnnex.isPending;

  if (!allowed) {
    return (
      <div className="page">
        <h1>Create an Annex</h1>
        <ErrorCallout error={new Error("Annexes are created by ArchiveAdmins and DataStewards.")} />
      </div>
    );
  }
  if (!filers.data || !datasets.data) return <Loading />;

  async function start() {
    if (!filer || !owners) return;
    let ds = datasetId;
    if (creating) {
      const made = await addDataset.mutateAsync({
        name: newDataset.name,
        backend_id: newDataset.backend_id,
        edge_id: filer.edge_id,
      });
      ds = made.dataset_id;
    }
    const a = await addAnnex.mutateAsync({
      name,
      filer_id: filer.filer_id,
      path: path.replace(/^\/+|\/+$/g, ""),
      // Custom starts as Copy, then opens the rule builder.
      mode: mode === "custom" ? "copy" : mode,
      ...(mode === "archive_after" ? { archive_after_days: days } : {}),
      dataset_id: ds,
      owners_sid: owners.sid,
      ...(Object.keys(settings).length ? { settings } : {}),
    });
    void navigate(mode === "custom" ? `/annexes/${a.annex_id}/rules` : `/annexes/${a.annex_id}`);
  }

  const d = defaults.data;
  return (
    <div className="page">
      <div className="page-head">
        <div>
          <p className="crumbs">
            <Link to="/annexes">Annexes</Link>
          </p>
          <h1>Create an Annex</h1>
          <p className="page-question">Which NAS folder should OmniFile copy to Azure?</p>
          <p className="lede">People keep using the NAS directly; nothing on it is changed.</p>
        </div>
      </div>

      <Question n={1} title="Which share?">
        <div className="field">
          <label className="field-label" htmlFor={ids.filer}>
            Filer
          </label>
          <select id={ids.filer} value={filerId} onChange={(e) => setFilerId(e.target.value)}>
            <option value="">Choose a filer</option>
            {filers.data.items.map((f) => (
              <option key={f.filer_id} value={f.filer_id}>
                {f.name} ({label(PROTOCOL_LABEL, f.protocol)} {f.host}
                {f.protocol === "smb" ? `\\${f.export}` : `:${f.export}`})
              </option>
            ))}
          </select>
        </div>
        {admin ? (
          addingFiler ? (
            <AddFiler
              onAdded={(id) => {
                setFilerId(id);
                setAddingFiler(false);
              }}
            />
          ) : (
            <button type="button" className="btn btn-link" onClick={() => setAddingFiler(true)}>
              Add a filer
            </button>
          )
        ) : null}
        {filer?.host_trusted ? (
          <p className="callout callout-warn">
            This filer is host-trusted (NFS sec=sys): it trusts the edge's address, not a proven
            identity.
          </p>
        ) : null}
        <div className="field">
          <label className="field-label" htmlFor={ids.path}>
            Folder on the share
          </label>
          <input
            id={ids.path}
            value={path}
            onChange={(e) => setPath(e.target.value)}
            placeholder="long-term-storage"
          />
          <p className="field-hint">
            Leave empty for the whole share. The edge mounts it read-only.
          </p>
        </div>
      </Question>

      <Question n={2} title="What should happen to files there?">
        <fieldset className="options">
          <legend className="sr-only">What happens to files</legend>
          {(
            [
              [
                "copy",
                "Copy",
                "Every file is copied to Azure, and copied again when it changes. Files deleted on the NAS keep their copy.",
              ],
              [
                "archive_after",
                "Archive after N days",
                "As Copy, then each file leaves the NAS N days after it arrived. A pointer file stays in its place.",
              ],
              [
                "move_now",
                "Move now",
                "A drop folder: each file leaves the NAS once its copy is verified.",
              ],
              ["custom", "Custom rules", "Starts as Copy, then opens the rule builder."],
            ] as const
          ).map(([v, t, hint]) => (
            <label className="option" key={v}>
              <input
                type="radio"
                name="mode"
                value={v}
                checked={mode === v}
                onChange={() => setMode(v)}
              />
              <span className="option-body">
                <span className="option-title">{t}</span>
                {hint}
              </span>
            </label>
          ))}
        </fieldset>
        {mode === "archive_after" ? (
          <div className="field">
            <label className="field-label" htmlFor={daysId}>
              Days after arrival
            </label>
            <input
              id={daysId}
              type="number"
              min={1}
              max={36500}
              value={days}
              onChange={(e) => setDays(Number(e.target.value))}
            />
          </div>
        ) : null}
      </Question>

      <Question n={4} title="Where do copies go?">
        <div className="field">
          <label className="field-label" htmlFor={ids.dataset}>
            Dataset
          </label>
          <select id={ids.dataset} value={datasetId} onChange={(e) => setDatasetId(e.target.value)}>
            <option value="">Choose a dataset</option>
            {usable.map((ds) => (
              <option key={ds.dataset_id} value={ds.dataset_id}>
                {ds.name}
              </option>
            ))}
            {admin ? <option value="new">A new dataset</option> : null}
          </select>
          <p className="field-hint">
            A dataset on the filer's edge that isn't shared. Its tier policy decides when copies
            move to Cool, Cold and Archive.
          </p>
        </div>
        {creating ? (
          <div className="grid-2">
            <div className="field">
              <label className="field-label" htmlFor={ids.dsName}>
                New dataset's name
              </label>
              <input
                id={ids.dsName}
                value={newDataset.name}
                onChange={(e) => setNewDataset((x) => ({ ...x, name: e.target.value }))}
              />
            </div>
            <div className="field">
              <label className="field-label" htmlFor={ids.backend}>
                Storage
              </label>
              <select
                id={ids.backend}
                value={newDataset.backend_id}
                onChange={(e) => setNewDataset((x) => ({ ...x, backend_id: e.target.value }))}
              >
                <option value="">Choose storage</option>
                {homes.map((b) => (
                  <option key={b.backend_id} value={b.backend_id}>
                    {b.name}
                  </option>
                ))}
              </select>
            </div>
          </div>
        ) : null}
      </Question>

      <Question n={5} title="Who looks after it?">
        <GroupPicker label="Owners group" onPick={setOwners} />
        <p>
          Owners: <strong>{groupName(owners)}</strong>
        </p>
        <p className="muted">People see files in the portal as the NAS's own permissions allow.</p>
        <div className="field">
          <label className="field-label" htmlFor={ids.name}>
            Name of the annex
          </label>
          <input id={ids.name} value={name} onChange={(e) => setName(e.target.value)} />
        </div>
      </Question>

      <section className="card card-pad">
        <button
          type="button"
          className="btn btn-link"
          aria-expanded={more}
          onClick={() => setMore(!more)}
        >
          More settings
        </button>
        {more && d ? (
          <div className="grid-2">
            {(
              [
                [ids.quiet, "quiet_minutes", "Quiet period (minutes) before a file is copied"],
                [ids.quick, "quick_scan_minutes", "Quick scan every (minutes)"],
                [ids.full, "full_scan_minutes", "Full scan every (minutes)"],
              ] as const
            ).map(([id, key, text]) => (
              <div className="field" key={key}>
                <label className="field-label" htmlFor={id}>
                  {text}
                </label>
                <input
                  id={id}
                  type="number"
                  min={1}
                  value={settings[key] ?? d[key]}
                  onChange={(e) => setSettings((s) => ({ ...s, [key]: Number(e.target.value) }))}
                />
              </div>
            ))}
          </div>
        ) : null}
      </section>

      <Question n={7} title="Start">
        <p>
          Copying starts at once: the first scan counts the files already there, then copies them.
        </p>
        {mode === "archive_after" || mode === "move_now" ? (
          <p className="callout callout-warn">
            Removals wait for an ArchiveAdmin&apos;s approval and a 7-day dry run, and start only
            when an ArchiveAdmin confirms the dry run. In this release nothing is removed from the
            NAS: the annex records what it would remove.
          </p>
        ) : null}
        {addAnnex.isError ? <ErrorCallout error={addAnnex.error} /> : null}
        {addDataset.isError ? <ErrorCallout error={addDataset.error} /> : null}
        <button
          type="button"
          className="btn btn-primary"
          disabled={!ready}
          onClick={() => void start()}
        >
          Start copying
        </button>
      </Question>
    </div>
  );
}
