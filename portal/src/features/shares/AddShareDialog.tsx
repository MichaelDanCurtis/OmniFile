import { useId, useState, type ReactNode } from "react";
import { Link } from "react-router";
import { ApiError } from "../../api/client";
import { useAddDataset, useAddShare, useBackends, useDatasets, useEdges } from "../../api/queries";
import type { Check, Dataset, GroupRef, NewShare, OnOpen, Share } from "../../api/types";
import { Callout, ErrorCallout } from "../../components/Feedback";
import { GroupPicker, groupName } from "../../components/GroupPicker";
import { Modal } from "../../components/Modal";
import { backendLabel } from "../../lib/format";
import { KERBEROS_NOTE } from "./ShareSettingsPage";

/** The share name: its Windows share name, NFS path and root folder (NewShare.name). */
const SHARE_NAME = /^[A-Za-z0-9][A-Za-z0-9._-]{0,79}$/;
const RESERVED = new Set(["global", "homes", "printers", "ipc$"]);
const DATASET_NAME = /^[A-Za-z0-9][A-Za-z0-9._-]{0,62}$/;
const KEY_PREFIX = /^(?:[A-Za-z0-9!_.*'()-]+\/)*$/;
/** What Ganesha's client list can carry; the service checks the same. */
const CLIENT = /^[A-Za-z0-9._:/-]{1,253}$/;

type Protocol = NewShare["protocols"][number];
type Krb = "krb5" | "krb5i" | "krb5p";

const PROTOCOLS: { value: Protocol; title: string; body: string }[] = [
  { value: "smb", title: "Windows (SMB)", body: "Windows and Mac desktops map it as a drive." },
  { value: "nfs4", title: "Linux/Unix (NFSv4)", body: "Recommended for Linux clients." },
  { value: "nfs3", title: "Linux/Unix (NFSv3)", body: "For older clients and some clusters." },
];

const KRB: { value: Krb; title: string; body: string }[] = [
  { value: "krb5", title: "krb5", body: "Proves who the user is." },
  { value: "krb5i", title: "krb5i", body: "Also checksums every request." },
  { value: "krb5p", title: "krb5p", body: "Also encrypts all traffic. Recommended." },
];

const ON_OPEN: { value: OnOpen; title: string; body: string }[] = [
  {
    value: "fail",
    title: "Tell them straight away",
    body: "The open fails fast with a note, and they get a Teams card. Best for people.",
  },
  {
    value: "wait",
    title: "Make the program wait",
    body: "The read waits until the file is back. Best for batch clusters.",
  },
  {
    value: "auto",
    title: "Wait only if it's quick",
    body: "Waits up to a limit, otherwise fails fast.",
  },
];

const TONE = { error: "danger", warning: "warn", info: "info" } as const;

/** A client entry that lets in more than named hosts or subnets (the service refuses it). */
function wildcard(c: string): boolean {
  const low = c.toLowerCase();
  return /[*?[]/.test(c) || c.endsWith("/0") || ["all", "everyone", "0.0.0.0", "::"].includes(low);
}

function hostsError(hosts: string[]): string | null {
  if (hosts.length === 0) return "List at least one host or subnet: sec=sys trusts them.";
  const wild = hosts.find(wildcard);
  if (wild) return `${wild} lets in any host. Name the hosts or subnets.`;
  const bad = hosts.find((h) => !CLIENT.test(h));
  if (bad) return `${bad} isn't a host name, address or subnet.`;
  return null;
}

function nameError(name: string): string | null {
  if (name === "") return null;
  if (!SHARE_NAME.test(name)) {
    return "Use 1 to 80 letters, digits, '.', '_' or '-', starting with a letter or digit.";
  }
  if (RESERVED.has(name.toLowerCase())) return `${name} is reserved by Samba. Pick another name.`;
  return null;
}

/** A folder of the dataset, from its root: `/projects/2026` (NewShare.path). */
function pathError(path: string): string | null {
  if (path === "") return "Name the folder, starting with /.";
  if (!path.startsWith("/"))
    return "Start at the dataset's root, with /: for example /projects/2026.";
  if (path === "/") return "/ is the dataset's root, which is already shared. Name a folder in it.";
  const parts = path.slice(1).split("/");
  if (parts.some((p) => p === "")) return "Leave out empty folder names (// or a trailing /).";
  if (parts.some((p) => p === "." || p === "..")) return "Name the folder without . or ..";
  if (path.length > 1024) return "At most 1024 characters.";
  return null;
}

/** The server's message for one field of a 400 invalid_request, if it named that field. */
function serverFieldError(error: unknown, field: string): string | null {
  if (!(error instanceof ApiError) || error.code !== "invalid_request") return null;
  return error.problem?.errors?.find((e) => e.field === field)?.message ?? null;
}

function days(value: string): number | null {
  const n = Number(value);
  return value.trim() !== "" && Number.isInteger(n) && n >= 0 && n <= 3650 ? n : null;
}

/** One of the share's three AD groups: chosen, or a search to choose one. */
function GroupField({
  title,
  hint,
  group,
  required,
  onChange,
}: {
  title: string;
  hint: string;
  group: GroupRef | null;
  required?: boolean;
  onChange: (group: GroupRef | null) => void;
}) {
  const [changing, setChanging] = useState(false);
  const headId = useId();
  return (
    <div className="field" role="group" aria-labelledby={headId}>
      <span id={headId} className="field-label">
        {title}
        {required ? "" : " (optional)"}
      </span>
      <span className="field-hint">{hint}</span>
      {group && !changing ? (
        <div className="row" style={{ gap: 8 }}>
          <strong>{groupName(group)}</strong>
          <span className="mono muted">{group.sam_account_name ?? group.sid}</span>
          <button type="button" className="btn btn-sm" onClick={() => setChanging(true)}>
            Change
          </button>
          {required ? null : (
            <button type="button" className="btn btn-sm btn-ghost" onClick={() => onChange(null)}>
              Remove
            </button>
          )}
        </div>
      ) : (
        <GroupPicker
          label={`${title} group`}
          onPick={(g) => {
            onChange(g);
            setChanging(false);
          }}
        />
      )}
    </div>
  );
}

function Created({
  share,
  dataset,
  edge,
  folder,
}: {
  share: Share;
  dataset: string;
  edge: string | null;
  folder: string | null;
}) {
  const host = edge ?? "<edge>";
  const nfs = share.protocols.some((p) => p !== "smb");
  const smb = share.protocols.includes("smb");
  return (
    <>
      <Callout tone="ok" title={`${share.name} is ready`} role="status">
        <p>
          The edge picks it up within a minute and gives{" "}
          {folder ? (
            <>
              the folder <code>{folder}</code>
            </>
          ) : (
            "its root folder"
          )}{" "}
          the groups&apos; access.
          {smb ? (
            <>
              {" "}
              Windows: <code>{`\\\\${host}\\${share.name}`}</code>.
            </>
          ) : null}
          {nfs ? (
            <>
              {" "}
              NFS: <code>{`${host}:/${share.name}`}</code>.
            </>
          ) : null}
        </p>
        <p className="muted">
          {folder
            ? `It serves ${folder} of the dataset ${dataset}.`
            : `Its data lives in the dataset ${dataset}.`}
        </p>
      </Callout>
      {share.checks.map((c: Check, i) => (
        <Callout key={`${c.code}-${i}`} tone={TONE[c.severity]}>
          {c.message}
        </Callout>
      ))}
      <p>
        <Link to={`/shares/${share.share_id}`}>Open {share.name}&apos;s settings</Link> to change
        any of this later, or <Link to={`/shares/${share.share_id}/access`}>its access</Link>.
      </p>
    </>
  );
}

function Section({ title, children }: { title: string; children: ReactNode }) {
  const id = useId();
  return (
    <section className="stack" aria-labelledby={id}>
      <h3 id={id}>{title}</h3>
      {children}
    </section>
  );
}

export function AddShareDialog({ open, onClose }: { open: boolean; onClose: () => void }) {
  const edges = useEdges(open);
  const backends = useBackends(open);
  const datasets = useDatasets(open);
  const addDataset = useAddDataset();
  const addShare = useAddShare();

  const [name, setName] = useState("");
  const [where, setWhere] = useState<"new" | "existing" | "folder">("new");
  const [edgeChoice, setEdgeChoice] = useState("");
  const [backendChoice, setBackendChoice] = useState("");
  const [existingId, setExistingId] = useState("");
  const [folderDatasetId, setFolderDatasetId] = useState("");
  const [folderPath, setFolderPath] = useState("");
  /** A dataset this dialog made whose share then failed: kept, so a retry reuses it. */
  const [made, setMade] = useState<Dataset | null>(null);
  const [protocols, setProtocols] = useState<Protocol[]>(["smb", "nfs4"]);
  const [auth, setAuth] = useState<"kerberos" | "sys">("kerberos");
  const [krb, setKrb] = useState<Krb>("krb5p");
  const [hostsText, setHostsText] = useState("");
  const [owners, setOwners] = useState<GroupRef | null>(null);
  const [writers, setWriters] = useState<GroupRef | null>(null);
  const [readers, setReaders] = useState<GroupRef | null>(null);
  const [onOpen, setOnOpen] = useState<OnOpen>("fail");
  const [threshold, setThreshold] = useState("15");
  const [smbEncrypt, setSmbEncrypt] = useState(false);
  const [depth, setDepth] = useState("0");
  const [maxDepth, setMaxDepth] = useState("0");
  const [layout, setLayout] = useState<"two_account" | "single_account">("two_account");
  const [keyPrefix, setKeyPrefix] = useState("");
  const [datasetName, setDatasetName] = useState("");
  const [override, setOverride] = useState(false);
  const [busy, setBusy] = useState(false);

  const nameId = useId();
  const nameHint = useId();
  const nameErrId = useId();
  const edgeId = useId();
  const backendId = useId();
  const existingSel = useId();
  const sharedSel = useId();
  const pathId = useId();
  const pathHint = useId();
  const pathErrId = useId();
  const hostsId = useId();
  const hostsHint = useId();
  const thresholdId = useId();
  const depthId = useId();
  const maxId = useId();
  const prefixId = useId();
  const prefixHint = useId();
  const dsNameId = useId();

  const edgeList = (edges.data?.edges ?? []).filter((e) => e.state !== "revoked");
  const homes = (backends.data?.backends ?? []).filter((b) => b.can_be_home);
  const s3Only = (backends.data?.backends ?? []).some((b) => !b.can_be_home);
  const listed = (datasets.data?.items ?? []).filter((d) => d.edge_id && d.shares.length === 0);
  // The dataset this dialog made is offered even before the list refetches.
  const unshared =
    made && !listed.some((d) => d.dataset_id === made.dataset_id) ? [made, ...listed] : listed;
  // A folder can be shared once its dataset's root is: offer datasets that have a share.
  const shared = (datasets.data?.items ?? []).filter((d) => d.edge_id && d.shares.length > 0);
  const edgeName = (id: string | null | undefined) =>
    edgeList.find((e) => e.edge_id === id)?.name ?? null;

  const edge = edgeChoice || (edgeList.length === 1 ? (edgeList[0]?.edge_id ?? "") : "");
  const backend = backendChoice || (homes.length === 1 ? (homes[0]?.backend_id ?? "") : "");
  const nfs = protocols.some((p) => p !== "smb");
  const sys = nfs && auth === "sys";
  const hosts = hostsText
    .split(/[\s,]+/)
    .map((h) => h.trim())
    .filter(Boolean);
  const dsName = datasetName || name.slice(0, 63);
  const depthN = days(depth);
  const maxN = days(maxDepth);
  const thresholdN = Number(threshold);
  const folderError = pathError(folderPath);
  const folderServerError = where === "folder" ? serverFieldError(addShare.error, "path") : null;

  const problems = {
    name: name === "" ? "Give the share a name." : nameError(name),
    where:
      where === "new"
        ? !edge
          ? "Choose the edge that serves it."
          : !backend
            ? "Choose where its data is stored."
            : !DATASET_NAME.test(dsName)
              ? "The dataset's name: 1 to 63 letters, digits, '.', '_' or '-'."
              : !KEY_PREFIX.test(keyPrefix)
                ? "The key prefix: empty, or folders each ending in '/'."
                : null
        : where === "folder"
          ? !folderDatasetId
            ? "Choose a shared dataset."
            : folderError
          : existingId
            ? null
            : "Choose a dataset.",
    protocols: protocols.length === 0 ? "Choose at least one way to connect." : null,
    hosts: sys ? hostsError(hosts) : null,
    owners: owners ? null : "Choose the Owners group.",
    threshold:
      onOpen === "auto" && !(Number.isInteger(thresholdN) && thresholdN >= 1 && thresholdN <= 1440)
        ? "Minutes, 1 to 1440."
        : null,
    rewind:
      depthN === null || maxN === null
        ? "Rewind days: 0 to 3650."
        : depthN > maxN
          ? "Rewind depth can't be more than the most Owners may choose."
          : null,
  };
  const missing = Object.values(problems).filter((p): p is string => p !== null);
  // The footer says what is left, except what a field already says next to itself.
  const left = missing.find(
    (p) =>
      p !== problems.hosts &&
      p !== nameError(name) &&
      !(where === "folder" && folderPath !== "" && p === folderError),
  );

  function reset() {
    addDataset.reset();
    addShare.reset();
    setName("");
    setWhere("new");
    setEdgeChoice("");
    setBackendChoice("");
    setExistingId("");
    setFolderDatasetId("");
    setFolderPath("");
    setMade(null);
    setProtocols(["smb", "nfs4"]);
    setAuth("kerberos");
    setKrb("krb5p");
    setHostsText("");
    setOwners(null);
    setWriters(null);
    setReaders(null);
    setOnOpen("fail");
    setThreshold("15");
    setSmbEncrypt(false);
    setDepth("0");
    setMaxDepth("0");
    setLayout("two_account");
    setKeyPrefix("");
    setDatasetName("");
    setOverride(false);
  }

  function close() {
    reset();
    onClose();
  }

  function shareBody(datasetId: string): NewShare {
    const body: NewShare = {
      dataset_id: datasetId,
      name,
      protocols: PROTOCOLS.map((p) => p.value).filter((p) => protocols.includes(p)),
      owners_group_sid: owners?.sid ?? "",
      on_open: onOpen,
      smb_encrypt: protocols.includes("smb") && smbEncrypt,
      rewind_depth_days: depthN ?? 0,
      rewind_depth_max_days: maxN ?? 0,
    };
    if (where === "folder") body.path = folderPath;
    if (writers) body.writers_group_sid = writers.sid;
    if (readers) body.readers_group_sid = readers.sid;
    if (onOpen === "auto") body.wait_threshold_minutes = thresholdN;
    if (nfs) {
      body.nfs_sec = sys ? ["sys"] : [krb];
      if (sys) {
        body.nfs_clients = hosts;
        body.root_squash = true;
        if (override) body.host_trust_override = true;
      }
    }
    return body;
  }

  async function submit() {
    if (missing.length > 0 || busy) return;
    setBusy(true);
    addDataset.reset();
    addShare.reset();
    try {
      let datasetId = where === "folder" ? folderDatasetId : existingId;
      if (where === "new") {
        const dataset = await addDataset.mutateAsync({
          name: dsName,
          backend_id: backend,
          edge_id: edge,
          storage_layout: layout,
          key_prefix: keyPrefix,
        });
        // From here on the form points at this dataset, so a retry after a failed
        // share reuses it instead of making a second one.
        setMade(dataset);
        setWhere("existing");
        setExistingId(dataset.dataset_id);
        datasetId = dataset.dataset_id;
      }
      await addShare.mutateAsync(shareBody(datasetId));
    } catch {
      // Shown from the mutation's error below.
    } finally {
      setBusy(false);
    }
  }

  const created = addShare.data;
  const createdDataset =
    [...unshared, ...shared].find((d) => d.dataset_id === created?.dataset_id)?.name ??
    created?.dataset_name ??
    dsName;

  return (
    <Modal
      open={open}
      onClose={close}
      wide
      title="Add a share"
      description="A new NFS or SMB share on an edge, with its own folder and AD groups."
      footer={
        created ? (
          <button type="button" className="btn btn-primary" onClick={close}>
            Done
          </button>
        ) : (
          <>
            <span className="muted spacer" aria-live="polite">
              {left && name !== "" ? left : ""}
            </span>
            <button type="button" className="btn" onClick={close}>
              Cancel
            </button>
            <button
              type="button"
              className="btn btn-primary"
              disabled={missing.length > 0 || busy}
              onClick={() => void submit()}
            >
              {busy ? "Creating…" : "Create share"}
            </button>
          </>
        )
      }
    >
      {created ? (
        <Created
          share={created}
          dataset={createdDataset}
          edge={edgeName(created.edge_id)}
          folder={where === "folder" ? folderPath : null}
        />
      ) : (
        <>
          <div className="field">
            <label className="field-label" htmlFor={nameId}>
              Share name
            </label>
            <input
              id={nameId}
              type="text"
              value={name}
              maxLength={80}
              autoComplete="off"
              aria-describedby={`${nameHint}${problems.name && name ? ` ${nameErrId}` : ""}`}
              aria-invalid={name !== "" && nameError(name) !== null}
              onChange={(e) => setName(e.target.value.trim())}
            />
            <span id={nameHint} className="field-hint">
              People see this name: it is the Windows share name (<code>\\edge\name</code>) and the
              NFS path (<code>edge:/name</code>).
              {where === "folder"
                ? " The folder keeps its own name."
                : " It also names the dataset's root folder on the edge."}
            </span>
            {name && nameError(name) ? (
              <span id={nameErrId} className="field-error">
                {nameError(name)}
              </span>
            ) : null}
          </div>

          <Section title="Where does its data live?">
            <fieldset className="options">
              <legend className="sr-only">Where the data lives</legend>
              <label className="option">
                <input
                  type="radio"
                  name="where"
                  checked={where === "new"}
                  onChange={() => setWhere("new")}
                />
                <span className="option-title">In a new dataset</span>
                <span className="option-body">
                  {dsName ? (
                    <>
                      A dataset named <strong>{dsName}</strong>, with its own archive policy.
                    </>
                  ) : (
                    "A dataset named like the share, with its own archive policy."
                  )}
                </span>
              </label>
              <label className="option">
                <input
                  type="radio"
                  name="where"
                  checked={where === "existing"}
                  disabled={unshared.length === 0}
                  onChange={() => setWhere("existing")}
                />
                <span className="option-title">In a dataset that has no share yet</span>
                <span className="option-body">
                  {unshared.length === 0
                    ? "None: every dataset on an edge already has a share."
                    : "For example, one a migration seeded."}
                </span>
              </label>
              <label className="option">
                <input
                  type="radio"
                  name="where"
                  checked={where === "folder"}
                  disabled={shared.length === 0}
                  onChange={() => setWhere("folder")}
                />
                <span className="option-title">A folder in a shared dataset</span>
                <span className="option-body">
                  {shared.length === 0
                    ? "None yet: share a dataset's root first, then its folders."
                    : "Share one existing folder on its own, with its own groups and policy."}
                </span>
              </label>
            </fieldset>
            {where === "new" ? (
              <div className="grid-2">
                <div className="field">
                  <label className="field-label" htmlFor={edgeId}>
                    Which edge serves it?
                  </label>
                  <select id={edgeId} value={edge} onChange={(e) => setEdgeChoice(e.target.value)}>
                    <option value="">Choose an edge</option>
                    {edgeList.map((e) => (
                      <option key={e.edge_id} value={e.edge_id}>
                        {e.name}
                        {e.state === "pending" ? " (not enrolled yet)" : ""}
                      </option>
                    ))}
                  </select>
                </div>
                <div className="field">
                  <label className="field-label" htmlFor={backendId}>
                    Where is it stored?
                  </label>
                  <select
                    id={backendId}
                    value={backend}
                    onChange={(e) => setBackendChoice(e.target.value)}
                  >
                    <option value="">Choose storage</option>
                    {homes.map((b) => (
                      <option key={b.backend_id} value={b.backend_id}>
                        {b.name} ({backendLabel(b.kind)}
                        {b.private_network ? ", private endpoint" : ""})
                      </option>
                    ))}
                  </select>
                  {s3Only ? (
                    <span className="field-hint">
                      Amazon S3 isn&apos;t offered: it holds data only while it migrates.
                    </span>
                  ) : null}
                </div>
              </div>
            ) : where === "folder" ? (
              <div className="grid-2">
                <div className="field">
                  <label className="field-label" htmlFor={sharedSel}>
                    Shared dataset
                  </label>
                  <select
                    id={sharedSel}
                    value={folderDatasetId}
                    onChange={(e) => setFolderDatasetId(e.target.value)}
                  >
                    <option value="">Choose a dataset</option>
                    {shared.map((d) => (
                      <option key={d.dataset_id} value={d.dataset_id}>
                        {d.name} (shared as {d.shares.map((x) => x.name).join(", ")}
                        {edgeName(d.edge_id) ? `, on ${edgeName(d.edge_id)}` : ""})
                      </option>
                    ))}
                  </select>
                </div>
                <div className="field">
                  <label className="field-label" htmlFor={pathId}>
                    Folder
                  </label>
                  <input
                    id={pathId}
                    type="text"
                    className="mono"
                    value={folderPath}
                    maxLength={1024}
                    autoComplete="off"
                    placeholder="/projects/2026"
                    aria-describedby={`${pathHint}${
                      (folderPath && folderError) || folderServerError ? ` ${pathErrId}` : ""
                    }`}
                    aria-invalid={
                      (folderPath !== "" && folderError !== null) || !!folderServerError
                    }
                    onChange={(e) => {
                      setFolderPath(e.target.value.trim());
                      if (addShare.isError) addShare.reset();
                    }}
                  />
                  <span id={pathHint} className="field-hint">
                    From the dataset&apos;s root, for example <code>/projects/2026</code>. The
                    folder must already exist. Sharing a folder gives it its own groups and policy.
                  </span>
                  {(folderPath && folderError) || folderServerError ? (
                    <span id={pathErrId} className="field-error">
                      {folderPath && folderError ? folderError : folderServerError}
                    </span>
                  ) : null}
                </div>
              </div>
            ) : (
              <div className="field">
                <label className="field-label" htmlFor={existingSel}>
                  Dataset
                </label>
                <select
                  id={existingSel}
                  value={existingId}
                  onChange={(e) => setExistingId(e.target.value)}
                >
                  <option value="">Choose a dataset</option>
                  {unshared.map((d) => (
                    <option key={d.dataset_id} value={d.dataset_id}>
                      {d.name} ({backendLabel(d.backend_kind)}
                      {edgeName(d.edge_id) ? `, on ${edgeName(d.edge_id)}` : ""})
                    </option>
                  ))}
                </select>
                {made ? (
                  <span className="field-hint">
                    {made.name} was created for this share; it is used again when you retry.
                  </span>
                ) : null}
              </div>
            )}
          </Section>

          <Section title="Who connects?">
            <fieldset className="options options-row">
              <legend className="sr-only">Protocols</legend>
              {PROTOCOLS.map((p) => (
                <label key={p.value} className="option">
                  <input
                    type="checkbox"
                    checked={protocols.includes(p.value)}
                    onChange={(e) =>
                      setProtocols(
                        e.target.checked
                          ? [...protocols, p.value]
                          : protocols.filter((v) => v !== p.value),
                      )
                    }
                  />
                  <span className="option-title">{p.title}</span>
                  <span className="option-body">{p.body}</span>
                </label>
              ))}
            </fieldset>
          </Section>

          {nfs ? (
            <Section title="How do NFS clients prove who they are?">
              <fieldset className="options">
                <legend className="sr-only">NFS security</legend>
                <label className="option">
                  <input
                    type="radio"
                    name="nfs-auth"
                    checked={auth === "kerberos"}
                    onChange={() => setAuth("kerberos")}
                  />
                  <span className="option-title">Kerberos</span>
                  <span className="option-body">
                    Each user is proven by AD. Recommended; clients need a Kerberos setup.
                  </span>
                </label>
                <label className="option">
                  <input
                    type="radio"
                    name="nfs-auth"
                    checked={auth === "sys"}
                    onChange={() => setAuth("sys")}
                  />
                  <span className="option-title">Trust named hosts (sec=sys)</span>
                  <span className="option-body">
                    The client says who the user is. Only the hosts you list may connect.
                  </span>
                </label>
              </fieldset>
              {auth === "kerberos" ? (
                <>
                  <fieldset className="options options-row">
                    <legend className="field-label">Kerberos level</legend>
                    {KRB.map((k) => (
                      <label key={k.value} className="option">
                        <input
                          type="radio"
                          name="krb"
                          checked={krb === k.value}
                          onChange={() => setKrb(k.value)}
                        />
                        <span className="option-title mono">{k.title}</span>
                        <span className="option-body">{k.body}</span>
                      </label>
                    ))}
                  </fieldset>
                  <Callout tone="info" title="Kerberos and throughput">
                    {KERBEROS_NOTE}
                  </Callout>
                </>
              ) : (
                <div className="field">
                  <label className="field-label" htmlFor={hostsId}>
                    Trusted hosts and subnets
                  </label>
                  <textarea
                    id={hostsId}
                    className="mono"
                    value={hostsText}
                    aria-describedby={hostsHint}
                    aria-invalid={hostsText !== "" && problems.hosts !== null}
                    placeholder={"10.20.0.0/16\nhpc-login1.example.com"}
                    onChange={(e) => setHostsText(e.target.value)}
                  />
                  <span id={hostsHint} className="field-hint">
                    One per line. No wildcards. Root on these hosts is always mapped to nobody (root
                    squash).
                  </span>
                  {problems.hosts ? (
                    <span className="field-error" role="status">
                      {problems.hosts}
                    </span>
                  ) : null}
                </div>
              )}
            </Section>
          ) : null}

          <Section title="Who has access?">
            <GroupField
              title="Owners"
              required
              hint="Read and write; set rewind, what people see on archived files, and the other groups."
              group={owners}
              onChange={setOwners}
            />
            <GroupField
              title="Writers"
              hint="Read and write; restore old versions."
              group={writers}
              onChange={setWriters}
            />
            <GroupField
              title="Readers"
              hint="Read, recall ahead of time, rewind and download."
              group={readers}
              onChange={setReaders}
            />
          </Section>

          <Section title="When someone opens an archived file">
            <fieldset className="options">
              <legend className="sr-only">Open behaviour</legend>
              {ON_OPEN.map((o) => (
                <label key={o.value} className="option">
                  <input
                    type="radio"
                    name="on_open"
                    checked={onOpen === o.value}
                    onChange={() => setOnOpen(o.value)}
                  />
                  <span className="option-title">{o.title}</span>
                  <span className="option-body">{o.body}</span>
                </label>
              ))}
            </fieldset>
            {onOpen === "auto" ? (
              <div className="sentence">
                <label htmlFor={thresholdId}>Wait at most</label>
                <input
                  id={thresholdId}
                  className="inline-num"
                  type="number"
                  min={1}
                  max={1440}
                  value={threshold}
                  aria-invalid={problems.threshold !== null}
                  onChange={(e) => setThreshold(e.target.value)}
                />
                <span>minutes</span>
              </div>
            ) : null}
          </Section>

          <details className="more">
            <summary>More settings</summary>
            <div className="more-body stack">
              <label className="check-row">
                <input
                  type="checkbox"
                  checked={smbEncrypt}
                  disabled={!protocols.includes("smb")}
                  onChange={(e) => setSmbEncrypt(e.target.checked)}
                />
                <span>
                  Require SMB encryption
                  <span className="field-hint">
                    Clients without SMB3 encryption can&apos;t connect.
                  </span>
                </span>
              </label>
              <div className="sentence">
                <label htmlFor={depthId}>Let people rewind</label>
                <input
                  id={depthId}
                  className="inline-num"
                  type="number"
                  min={0}
                  max={3650}
                  value={depth}
                  aria-invalid={problems.rewind !== null}
                  onChange={(e) => setDepth(e.target.value)}
                />
                <span>days;</span>
                <label htmlFor={maxId}>Owners may choose up to</label>
                <input
                  id={maxId}
                  className="inline-num"
                  type="number"
                  min={0}
                  max={3650}
                  value={maxDepth}
                  aria-invalid={problems.rewind !== null}
                  onChange={(e) => setMaxDepth(e.target.value)}
                />
                <span>days.</span>
              </div>
              <p className="field-hint">
                0 turns rewind off. Old versions are billed at full size, so a longer rewind costs
                more storage.
              </p>
              {where === "new" ? (
                <>
                  <div className="field">
                    <label className="field-label" htmlFor={dsNameId}>
                      Dataset name
                    </label>
                    <input
                      id={dsNameId}
                      type="text"
                      value={datasetName}
                      placeholder={name.slice(0, 63) || "Same as the share"}
                      maxLength={63}
                      aria-invalid={datasetName !== "" && !DATASET_NAME.test(datasetName)}
                      onChange={(e) => setDatasetName(e.target.value.trim())}
                    />
                  </div>
                  <fieldset className="options">
                    <legend className="field-label">Storage layout</legend>
                    <label className="option">
                      <input
                        type="radio"
                        name="layout"
                        checked={layout === "two_account"}
                        onChange={() => setLayout("two_account")}
                      />
                      <span className="option-title">Live account plus archive account</span>
                      <span className="option-body">
                        The default. Allows Smart tier; files move to Archive by copy.
                      </span>
                    </label>
                    <label className="option">
                      <input
                        type="radio"
                        name="layout"
                        checked={layout === "single_account"}
                        onChange={() => setLayout("single_account")}
                      />
                      <span className="option-title">One account, tiered in place</span>
                      <span className="option-body">
                        Needed if this share will be WORM later. No Smart tier.
                      </span>
                    </label>
                  </fieldset>
                  <div className="field">
                    <label className="field-label" htmlFor={prefixId}>
                      Key prefix
                    </label>
                    <input
                      id={prefixId}
                      type="text"
                      className="mono"
                      value={keyPrefix}
                      maxLength={512}
                      placeholder="projects/seismic/"
                      aria-describedby={prefixHint}
                      aria-invalid={!KEY_PREFIX.test(keyPrefix)}
                      onChange={(e) => setKeyPrefix(e.target.value.trim())}
                    />
                    <span id={prefixHint} className="field-hint">
                      Where its blobs go in the storage account. Empty, or folders each ending in
                      &apos;/&apos;.
                    </span>
                  </div>
                </>
              ) : null}
              <label className="check-row">
                <input
                  type="checkbox"
                  checked={override}
                  disabled={!sys}
                  onChange={(e) => setOverride(e.target.checked)}
                />
                <span>
                  Allow host-trusted access even where folders carry restricted groups
                  <span className="field-hint">
                    Only for sec=sys. Recorded in the audit log; the portal shows it as a warning.
                  </span>
                </span>
              </label>
            </div>
          </details>
        </>
      )}
      {addDataset.isError ? (
        <ErrorCallout error={addDataset.error} title="The dataset wasn't created" />
      ) : null}
      {addShare.isError ? (
        <>
          <ErrorCallout error={addShare.error} title="The share wasn't created" />
          {made ? (
            <Callout tone="info">
              The dataset {made.name} was created and is kept. Fix the share and try again: it is
              used, not made twice.
            </Callout>
          ) : null}
        </>
      ) : null}
    </Modal>
  );
}
