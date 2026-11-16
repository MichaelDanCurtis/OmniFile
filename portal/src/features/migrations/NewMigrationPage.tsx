import { useEffect, useId, useRef, useState, type ReactNode } from "react";
import { Link, useNavigate, useSearchParams } from "react-router";
import { useAddDataset, useBackends, useDatasets, useEdges } from "../../api/queries";
import type { Dataset, GroupRef } from "../../api/types";
import { useSession } from "../../app/session";
import { usePageTitle } from "../../app/usePageTitle";
import { Callout, ErrorCallout, Loading } from "../../components/Feedback";
import { groupName } from "../../components/GroupPicker";
import { CheckIcon } from "../../components/icons";
import { backendLabel } from "../../lib/format";
import { isArchiveAdmin } from "../../lib/permissions";
import {
  useRegisterSource,
  useSourceBackends,
  type SourceBackend,
  type SourceCreate,
  type SourceInventory,
  type SourceScanCreate,
} from "./api";
import { AnnexSource } from "../annex/AnnexSource";
import { GroupField } from "./GroupField";
import { ScanPanel, ScanSummary } from "./ScanPanel";
import { DEFAULT_HOW, HowFields, type How } from "./HowFields";
import { copierLabel } from "./labels";
import { isArmed, loadHow, markArmed, saveHow, StartPanel } from "./StartPanel";

/**
 * New migration (portal-api.md 5.10, 5.17): plain questions, every setting one
 * click down. Start registers the source (POST /v1/sources), waits for it to
 * be catalogued, and starts its migration (POST /v1/migrations). The source is
 * kept if the second call fails, and `?source=<id>` resumes from it.
 */

const STEPS = [
  { key: "source", name: "Source" },
  { key: "destination", name: "Destination" },
  { key: "access", name: "Access" },
  { key: "how", name: "How" },
  { key: "review", name: "Review" },
] as const;
type StepKey = (typeof STEPS)[number]["key"];

/**
 * Where data can come from. Each kind brings its own questions. An Annex (docs/design/annex.md) is set up on
 * its own page, Create an Annex, which this hands over to.
 */
const SOURCE_KINDS = [
  {
    kind: "s3",
    title: "An Amazon S3 bucket",
    body: "Objects in a bucket an ArchiveAdmin set up as a backend, all of it or under a prefix.",
  },
  {
    kind: "annex",
    title: "A NAS share (Annex)",
    body: "A folder on an NFS or SMB server you run today, ONTAP and PowerScale included: its files are copied to Azure while people keep using the NAS.",
  },
] as const;
type SourceKind = (typeof SOURCE_KINDS)[number]["kind"];

type InventoryChoice = "auto" | "listing" | "s3_inventory" | "s3_metadata";

const BUCKET = /^[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]$/;
const DATASET_NAME = /^[A-Za-z0-9][A-Za-z0-9._-]{0,62}$/;
const KEY_PREFIX = /^(?:[A-Za-z0-9!_.*'()-]+\/)*$/;
const TABLE_ARN = /^arn:aws[a-z-]*:s3tables:[a-z0-9-]+:[0-9]{12}:bucket\/[a-z0-9][a-z0-9-]{1,61}$/;
const NAMESPACE = /^[a-z0-9_]{1,255}$/;

interface Draft {
  kind: SourceKind;
  backendId: string;
  bucket: string;
  prefix: string;
  inventory: InventoryChoice;
  invBucket: string;
  invManifest: string;
  tableArn: string;
  namespace: string;
  destination: "existing" | "new";
  datasetId: string;
  newName: string;
  edgeId: string;
  homeId: string;
  layout: "two_account" | "single_account";
  keyPrefix: string;
  folder: string;
  owners: GroupRef | null;
  writers: GroupRef | null;
  readers: GroupRef | null;
}

const EMPTY: Draft = {
  kind: "s3",
  backendId: "",
  bucket: "",
  prefix: "",
  inventory: "auto",
  invBucket: "",
  invManifest: "",
  tableArn: "",
  namespace: "",
  destination: "existing",
  datasetId: "",
  newName: "",
  edgeId: "",
  homeId: "",
  layout: "two_account",
  keyPrefix: "",
  folder: "",
  owners: null,
  writers: null,
  readers: null,
};

/** A dataset folder from the root, as the API takes it: no leading or trailing '/'. */
function folderPath(folder: string): string {
  return folder.trim().replace(/^\/+|\/+$/g, "");
}

function folderError(folder: string): string | null {
  const path = folderPath(folder);
  if (path === "") return null;
  const parts = path.split("/");
  if (parts.some((p) => p === "")) return "Leave out empty folder names (//).";
  if (parts.some((p) => p === "." || p === "..")) return "Name the folder without . or ..";
  if (path.length > 1024) return "At most 1024 characters.";
  return null;
}

/** A dataset the source can land in: an Azure home that isn't moving elsewhere. */
function eligible(d: Dataset): boolean {
  return (
    d.backend_kind !== "s3" &&
    (d.migration_state === undefined ||
      d.migration_state === "steady" ||
      d.migration_state === "migrating")
  );
}

function Question({ n, title, children }: { n: number; title: string; children: ReactNode }) {
  const id = useId();
  const ref = useRef<HTMLHeadingElement>(null);
  useEffect(() => {
    ref.current?.focus();
  }, []);
  return (
    <section className="question" aria-labelledby={id}>
      <span className="q-num" aria-hidden="true">
        {n}
      </span>
      <h2 id={id} ref={ref} tabIndex={-1}>
        {title}
      </h2>
      <div className="question-body">{children}</div>
    </section>
  );
}

function More({ children }: { children: ReactNode }) {
  return (
    <details className="more">
      <summary>More settings</summary>
      <div className="more-body stack">{children}</div>
    </details>
  );
}

function Stepper({ at }: { at: number }) {
  return (
    <ol
      className="steps"
      aria-label="New migration"
      style={{ gridTemplateColumns: `repeat(${STEPS.length}, minmax(0, 1fr))` }}
    >
      {STEPS.map((s, i) => {
        const status = i < at ? "done" : i === at ? "current" : "pending";
        return (
          <li
            key={s.key}
            className="step"
            data-status={status}
            aria-current={status === "current" ? "step" : undefined}
          >
            <span className="step-dot" aria-hidden="true">
              {status === "done" ? <CheckIcon /> : i + 1}
            </span>
            <span className="step-name">{s.name}</span>
          </li>
        );
      })}
    </ol>
  );
}

export function NewMigrationPage() {
  usePageTitle("New migration");
  const { me } = useSession();
  const admin = isArchiveAdmin(me);
  const [params, setParams] = useSearchParams();
  const resumeId = params.get("source");

  if (!admin) {
    return (
      <div className="page">
        <h1>New migration</h1>
        <ErrorCallout error={new Error("Migrations are for ArchiveAdmin.")} />
      </div>
    );
  }
  return (
    <div className="page">
      <nav aria-label="Breadcrumb">
        <ol className="crumbs">
          <li>
            <Link to="/migrations">Migrations</Link>
          </li>
          <li>
            <span aria-current="page">New migration</span>
          </li>
        </ol>
      </nav>
      {resumeId ? (
        <Resume key={resumeId} sourceId={resumeId} />
      ) : (
        <Wizard onRegistered={(id) => setParams({ source: id }, { replace: true })} />
      )}
    </div>
  );
}

/** A source registered earlier, by this wizard or another: start its migration. */
function Resume({ sourceId }: { sourceId: string }) {
  const [how, setHow] = useState<How>(() => loadHow(sourceId) ?? DEFAULT_HOW);
  // Armed when this tab registered the source a moment ago (its choices are saved).
  const [armed, setArmed] = useState(() => isArmed(sourceId));
  return (
    <>
      <div className="page-head">
        <div>
          <h1>New migration</h1>
          <p className="page-question">What happens next with this source?</p>
          <p className="lede">
            The source is registered. Once OmniFile has catalogued it, its migration starts.
          </p>
        </div>
      </div>
      <div className="card card-pad">
        <StartPanel
          sourceId={sourceId}
          how={how}
          onHow={setHow}
          armed={armed}
          onArm={(next) => {
            setArmed(next);
            if (next) saveHow(sourceId, how);
          }}
        />
      </div>
    </>
  );
}

function Wizard({ onRegistered }: { onRegistered: (sourceId: string) => void }) {
  const backends = useSourceBackends();
  const homes = useBackends();
  const datasets = useDatasets();
  const edges = useEdges();
  const addDataset = useAddDataset();
  const register = useRegisterSource();
  const navigate = useNavigate();

  const [at, setAt] = useState(0);
  const [d, setD] = useState<Draft>(EMPTY);
  const [how, setHow] = useState<How>(DEFAULT_HOW);
  /** A dataset this wizard made whose source then failed: kept, so a retry reuses it. */
  const [made, setMade] = useState<Dataset | null>(null);
  const [busy, setBusy] = useState(false);
  /** The latest source scan this wizard ran (cloud-connect.md 4). */
  const [scanId, setScanId] = useState<string | null>(null);
  const set = (patch: Partial<Draft>) => setD((prev) => ({ ...prev, ...patch }));

  const ids = {
    backend: useId(),
    bucket: useId(),
    bucketHint: useId(),
    prefix: useId(),
    prefixHint: useId(),
    invBucket: useId(),
    invManifest: useId(),
    tableArn: useId(),
    namespace: useId(),
    dataset: useId(),
    newName: useId(),
    edge: useId(),
    home: useId(),
    keyPrefix: useId(),
    folder: useId(),
    folderHint: useId(),
    kindName: useId(),
    invName: useId(),
    destName: useId(),
    layoutName: useId(),
  };

  const s3 = backends.data?.backends ?? [];
  const backendId = d.backendId || (s3.length === 1 ? (s3[0]?.backend_id ?? "") : "");
  const backend: SourceBackend | undefined = s3.find((b) => b.backend_id === backendId);
  const bucketList = backend?.buckets ?? null;
  const bucket = d.bucket || (bucketList?.length === 1 ? (bucketList[0] ?? "") : "");
  const hasTables = !!backend && backend.metadata_tables.includes(bucket);

  const listed = (datasets.data?.items ?? []).filter(eligible);
  const offered =
    made && !listed.some((x) => x.dataset_id === made.dataset_id) ? [made, ...listed] : listed;
  const homeList = (homes.data?.backends ?? []).filter((b) => b.can_be_home);
  const edgeList = (edges.data?.edges ?? []).filter((e) => e.state !== "revoked");
  const edgeId = d.edgeId || (edgeList.length === 1 ? (edgeList[0]?.edge_id ?? "") : "");
  const homeId = d.homeId || (homeList.length === 1 ? (homeList[0]?.backend_id ?? "") : "");
  const dataset = offered.find((x) => x.dataset_id === d.datasetId);
  const homeName = (id: string | undefined, kind?: string) =>
    homeList.find((b) => b.backend_id === id)?.name ?? backendLabel(kind ?? null);
  const destName = d.destination === "new" ? d.newName : (dataset?.name ?? "");
  const destBackend =
    d.destination === "new"
      ? homeName(homeId, "azure_blob")
      : homeName(dataset?.backend_id, dataset?.backend_kind);

  const problems: Record<StepKey, string | null> = {
    source:
      d.kind === "annex"
        ? "An annex is set up on its own page: continue there."
        : !backend
          ? s3.length === 0
            ? "No S3 backend is set up yet."
            : "Choose the S3 backend."
          : !bucket
            ? "Choose the bucket."
            : !BUCKET.test(bucket)
              ? "That isn't an S3 bucket name."
              : d.prefix && !d.prefix.endsWith("/")
                ? "A prefix ends in '/', for example raw/2019/."
                : d.inventory === "s3_inventory" && (!d.invBucket || !d.invManifest)
                  ? "Name the inventory's bucket and its manifest."
                  : d.inventory === "s3_metadata" && !TABLE_ARN.test(d.tableArn)
                    ? "The table bucket ARN looks like arn:aws:s3tables:<region>:<account>:bucket/<name>."
                    : d.inventory === "s3_metadata" && d.namespace && !NAMESPACE.test(d.namespace)
                      ? "The namespace: lowercase letters, digits and '_'."
                      : null,
    destination:
      d.destination === "existing"
        ? !dataset
          ? "Choose a dataset."
          : folderError(d.folder)
        : !DATASET_NAME.test(d.newName)
          ? "The dataset's name: 1 to 63 letters, digits, '.', '_' or '-'."
          : !edgeId
            ? "Choose the edge that serves it."
            : !homeId
              ? "Choose the Azure storage it lives on."
              : !KEY_PREFIX.test(d.keyPrefix)
                ? "The key prefix: empty, or folders each ending in '/'."
                : folderError(d.folder),
    access:
      !d.owners || !d.writers || !d.readers
        ? "Choose the Owners, Writers and Readers groups."
        : null,
    how: null,
    review: null,
  };
  // A scan needs the backend, the bucket and a valid prefix; the inventory only if it is named.
  const scanRequest: SourceScanCreate | null =
    d.kind === "s3" && backend && BUCKET.test(bucket) && (!d.prefix || d.prefix.endsWith("/"))
      ? {
          backend_id: backendId,
          bucket,
          prefix: d.prefix,
          ...(d.inventory === "s3_inventory" && d.invBucket && d.invManifest
            ? {
                inventory: {
                  kind: "s3_inventory" as const,
                  bucket: d.invBucket,
                  manifest_key: d.invManifest,
                },
              }
            : {}),
        }
      : null;
  const step = STEPS[at]?.key ?? "source";
  const problem = problems[step];

  function inventory(): SourceInventory | null {
    switch (d.inventory) {
      case "listing":
        return { kind: "listing" };
      case "s3_inventory":
        return { kind: "s3_inventory", bucket: d.invBucket, manifest_key: d.invManifest };
      case "s3_metadata":
        return {
          kind: "s3_metadata",
          table_bucket_arn: d.tableArn,
          ...(d.namespace ? { namespace: d.namespace } : {}),
        };
      default:
        return null;
    }
  }

  async function start() {
    if (busy) return;
    setBusy(true);
    addDataset.reset();
    register.reset();
    try {
      let datasetId = d.datasetId;
      if (d.destination === "new") {
        const created = await addDataset.mutateAsync({
          name: d.newName,
          backend_id: homeId,
          edge_id: edgeId,
          storage_layout: d.layout,
          key_prefix: d.keyPrefix,
        });
        // From here on the wizard points at this dataset, so a retry reuses it.
        setMade(created);
        set({ destination: "existing", datasetId: created.dataset_id });
        datasetId = created.dataset_id;
      }
      const body: SourceCreate = {
        dataset_id: datasetId,
        backend_id: backendId,
        container: bucket,
        prefix: d.prefix,
        path: folderPath(d.folder),
        inventory: inventory(),
        groups: {
          owners: d.owners?.sid ?? "",
          writers: d.writers?.sid ?? null,
          readers: d.readers?.sid ?? null,
        },
      };
      const source = await register.mutateAsync(body);
      saveHow(source.source_id, how);
      markArmed(source.source_id);
      onRegistered(source.source_id);
    } catch {
      // Shown from the mutations' errors below.
    } finally {
      setBusy(false);
    }
  }

  const next = () => setAt((i) => Math.min(i + 1, STEPS.length - 1));
  const back = () => setAt((i) => Math.max(i - 1, 0));

  if (backends.isPending || datasets.isPending) return <Loading />;

  return (
    <>
      <div className="page-head">
        <div>
          <h1>New migration</h1>
          <p className="page-question">What should move here, and from where?</p>
          <p className="lede">
            People keep one share throughout: a file is served from Azure only once it is copied and
            checked.
          </p>
        </div>
      </div>
      <Stepper at={at} />
      {backends.isError ? <ErrorCallout error={backends.error} /> : null}
      {datasets.isError ? <ErrorCallout error={datasets.error} /> : null}

      {step === "source" ? (
        <Question key="source" n={1} title="Where is the data now?">
          <fieldset className="options">
            <legend className="sr-only">Kind of source</legend>
            {SOURCE_KINDS.map((k) => (
              <label key={k.kind} className="option">
                <input
                  type="radio"
                  name={ids.kindName}
                  checked={d.kind === k.kind}
                  onChange={() => set({ kind: k.kind })}
                />
                <span className="option-title">{k.title}</span>
                <span className="option-body">{k.body}</span>
              </label>
            ))}
          </fieldset>
          {d.kind === "annex" ? <AnnexSource /> : null}
          {d.kind === "s3" ? (
            s3.length === 0 ? (
              <Callout tone="warn" title="No S3 backend is set up">
                An ArchiveAdmin adds the AWS account and its buckets in the setup wizard, with their
                private endpoint and credentials. The portal picks from those; it never makes one.
              </Callout>
            ) : (
              <>
                <div className="grid-2">
                  <div className="field">
                    <label className="field-label" htmlFor={ids.backend}>
                      S3 backend
                    </label>
                    <select
                      id={ids.backend}
                      value={backendId}
                      onChange={(e) => set({ backendId: e.target.value, bucket: "" })}
                    >
                      <option value="">Choose a backend</option>
                      {s3.map((b) => (
                        <option key={b.backend_id} value={b.backend_id}>
                          {b.name}
                          {b.region ? ` (${b.region}` : " ("}
                          {b.private_network ? ", private endpoint)" : ")"}
                        </option>
                      ))}
                    </select>
                  </div>
                  <div className="field">
                    <label className="field-label" htmlFor={ids.bucket}>
                      Bucket
                    </label>
                    {!backend || bucketList ? (
                      <select
                        id={ids.bucket}
                        value={bucket}
                        disabled={!backend}
                        onChange={(e) => set({ bucket: e.target.value })}
                      >
                        <option value="">
                          {backend ? "Choose a bucket" : "Choose the backend first"}
                        </option>
                        {(bucketList ?? []).map((b) => (
                          <option key={b} value={b}>
                            {b}
                          </option>
                        ))}
                      </select>
                    ) : (
                      <>
                        <input
                          id={ids.bucket}
                          type="text"
                          className="mono"
                          value={d.bucket}
                          disabled={!backend}
                          autoComplete="off"
                          aria-describedby={ids.bucketHint}
                          onChange={(e) => set({ bucket: e.target.value.trim() })}
                        />
                        <span id={ids.bucketHint} className="field-hint">
                          This backend takes any bucket its role can read.
                        </span>
                      </>
                    )}
                  </div>
                </div>
                <div className="field">
                  <label className="field-label" htmlFor={ids.prefix}>
                    Prefix (optional)
                  </label>
                  <input
                    id={ids.prefix}
                    type="text"
                    className="mono"
                    value={d.prefix}
                    maxLength={1024}
                    placeholder="raw/2019/"
                    autoComplete="off"
                    aria-describedby={ids.prefixHint}
                    onChange={(e) => set({ prefix: e.target.value.trim() })}
                  />
                  <span id={ids.prefixHint} className="field-hint">
                    Only objects under this prefix. Leave it empty for the whole bucket.
                  </span>
                </div>
                <More>
                  <fieldset className="options">
                    <legend className="field-label">How OmniFile finds the objects</legend>
                    {(
                      [
                        [
                          "auto",
                          "Automatically",
                          hasTables
                            ? "This bucket has S3 Metadata tables, so OmniFile reads those."
                            : "S3 Metadata tables where the backend has them, otherwise a listing.",
                        ],
                        [
                          "s3_inventory",
                          "From an S3 Inventory report",
                          "A CSV inventory's manifest. Good for very large buckets.",
                        ],
                        [
                          "s3_metadata",
                          "From S3 Metadata tables",
                          "The bucket's live inventory table, kept current from its journal.",
                        ],
                        ["listing", "List the prefix", "Slower for big buckets; always works."],
                      ] as const
                    ).map(([value, title, body]) => (
                      <label key={value} className="option">
                        <input
                          type="radio"
                          name={ids.invName}
                          checked={d.inventory === value}
                          onChange={() => set({ inventory: value })}
                        />
                        <span className="option-title">{title}</span>
                        <span className="option-body">{body}</span>
                      </label>
                    ))}
                  </fieldset>
                  {d.inventory === "s3_inventory" ? (
                    <div className="grid-2">
                      <div className="field">
                        <label className="field-label" htmlFor={ids.invBucket}>
                          Inventory bucket
                        </label>
                        <input
                          id={ids.invBucket}
                          type="text"
                          className="mono"
                          value={d.invBucket}
                          autoComplete="off"
                          onChange={(e) => set({ invBucket: e.target.value.trim() })}
                        />
                      </div>
                      <div className="field">
                        <label className="field-label" htmlFor={ids.invManifest}>
                          Manifest key
                        </label>
                        <input
                          id={ids.invManifest}
                          type="text"
                          className="mono"
                          value={d.invManifest}
                          placeholder="acme-seismic/daily/2026-10-14T01-00Z/manifest.json"
                          autoComplete="off"
                          onChange={(e) => set({ invManifest: e.target.value.trim() })}
                        />
                      </div>
                    </div>
                  ) : d.inventory === "s3_metadata" ? (
                    <div className="grid-2">
                      <div className="field">
                        <label className="field-label" htmlFor={ids.tableArn}>
                          Table bucket ARN
                        </label>
                        <input
                          id={ids.tableArn}
                          type="text"
                          className="mono"
                          value={d.tableArn}
                          placeholder="arn:aws:s3tables:us-east-1:123456789012:bucket/aws-s3"
                          autoComplete="off"
                          onChange={(e) => set({ tableArn: e.target.value.trim() })}
                        />
                      </div>
                      <div className="field">
                        <label className="field-label" htmlFor={ids.namespace}>
                          Namespace (optional)
                        </label>
                        <input
                          id={ids.namespace}
                          type="text"
                          className="mono"
                          value={d.namespace}
                          placeholder={bucket ? `b_${bucket.replace(/[^a-z0-9]/g, "_")}` : ""}
                          autoComplete="off"
                          onChange={(e) => set({ namespace: e.target.value.trim() })}
                        />
                      </div>
                    </div>
                  ) : null}
                </More>
                <ScanPanel
                  request={scanRequest}
                  scanId={scanId}
                  onScan={setScanId}
                  onUsePrefix={(prefix) => set({ prefix })}
                />
              </>
            )
          ) : null}
        </Question>
      ) : null}

      {step === "destination" ? (
        <Question key="destination" n={2} title="Which dataset does it go into?">
          <fieldset className="options">
            <legend className="sr-only">Destination</legend>
            <label className="option">
              <input
                type="radio"
                name={ids.destName}
                checked={d.destination === "existing"}
                disabled={offered.length === 0}
                onChange={() => set({ destination: "existing" })}
              />
              <span className="option-title">A dataset that exists</span>
              <span className="option-body">
                {offered.length === 0
                  ? "None can take a migration now."
                  : "Its files and the migrated ones live side by side, under the same policy."}
              </span>
            </label>
            <label className="option">
              <input
                type="radio"
                name={ids.destName}
                checked={d.destination === "new"}
                onChange={() => set({ destination: "new" })}
              />
              <span className="option-title">A new dataset</span>
              <span className="option-body">
                With its own archive policy. Add a share for it afterwards, so people can reach it.
              </span>
            </label>
          </fieldset>
          {d.destination === "existing" ? (
            <div className="field">
              <label className="field-label" htmlFor={ids.dataset}>
                Dataset
              </label>
              <select
                id={ids.dataset}
                value={d.datasetId}
                onChange={(e) => set({ datasetId: e.target.value })}
              >
                <option value="">Choose a dataset</option>
                {offered.map((x) => (
                  <option key={x.dataset_id} value={x.dataset_id}>
                    {x.name} ({homeName(x.backend_id, x.backend_kind)}
                    {x.migration_state === "migrating" ? ", already migrating" : ""})
                  </option>
                ))}
              </select>
              {dataset ? (
                <span className="field-hint">
                  Stored on {destBackend}
                  {dataset.migration_state === "migrating"
                    ? ". It already takes data from an S3 backend; a new source must come from the same one."
                    : "."}
                </span>
              ) : null}
              {made ? (
                <span className="field-hint">
                  {made.name} was created by this wizard; it is used again when you retry.
                </span>
              ) : null}
            </div>
          ) : (
            <div className="grid-2">
              <div className="field">
                <label className="field-label" htmlFor={ids.newName}>
                  Dataset name
                </label>
                <input
                  id={ids.newName}
                  type="text"
                  value={d.newName}
                  maxLength={63}
                  autoComplete="off"
                  aria-invalid={d.newName !== "" && !DATASET_NAME.test(d.newName)}
                  onChange={(e) => set({ newName: e.target.value.trim() })}
                />
              </div>
              <div className="field">
                <label className="field-label" htmlFor={ids.edge}>
                  Which edge serves it?
                </label>
                <select
                  id={ids.edge}
                  value={edgeId}
                  onChange={(e) => set({ edgeId: e.target.value })}
                >
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
                <label className="field-label" htmlFor={ids.home}>
                  Azure storage
                </label>
                <select
                  id={ids.home}
                  value={homeId}
                  onChange={(e) => set({ homeId: e.target.value })}
                >
                  <option value="">Choose storage</option>
                  {homeList.map((b) => (
                    <option key={b.backend_id} value={b.backend_id}>
                      {b.name} ({backendLabel(b.kind)}
                      {b.private_network ? ", private endpoint" : ""})
                    </option>
                  ))}
                </select>
              </div>
            </div>
          )}
          <More>
            <div className="field">
              <label className="field-label" htmlFor={ids.folder}>
                Folder in the dataset
              </label>
              <input
                id={ids.folder}
                type="text"
                className="mono"
                value={d.folder}
                placeholder="/ (the dataset's root)"
                maxLength={1024}
                autoComplete="off"
                aria-describedby={ids.folderHint}
                aria-invalid={folderError(d.folder) !== null}
                onChange={(e) => set({ folder: e.target.value })}
              />
              <span id={ids.folderHint} className="field-hint">
                Where the objects appear, for example <code>/seismic/raw</code>. Missing folders are
                made.
              </span>
            </div>
            {d.destination === "new" ? (
              <>
                <fieldset className="options">
                  <legend className="field-label">Storage layout</legend>
                  <label className="option">
                    <input
                      type="radio"
                      name={ids.layoutName}
                      checked={d.layout === "two_account"}
                      onChange={() => set({ layout: "two_account" })}
                    />
                    <span className="option-title">Live account plus archive account</span>
                    <span className="option-body">
                      The default. Allows Smart tier; files move to Archive by copy.
                    </span>
                  </label>
                  <label className="option">
                    <input
                      type="radio"
                      name={ids.layoutName}
                      checked={d.layout === "single_account"}
                      onChange={() => set({ layout: "single_account" })}
                    />
                    <span className="option-title">One account, tiered in place</span>
                    <span className="option-body">
                      Needed if a share of it will be WORM later. No Smart tier.
                    </span>
                  </label>
                </fieldset>
                <div className="field">
                  <label className="field-label" htmlFor={ids.keyPrefix}>
                    Key prefix
                  </label>
                  <input
                    id={ids.keyPrefix}
                    type="text"
                    className="mono"
                    value={d.keyPrefix}
                    placeholder="projects/seismic/"
                    maxLength={512}
                    autoComplete="off"
                    onChange={(e) => set({ keyPrefix: e.target.value.trim() })}
                  />
                </div>
              </>
            ) : null}
          </More>
        </Question>
      ) : null}

      {step === "access" ? (
        <Question key="access" n={3} title="Who may use the migrated files?">
          <p className="muted">
            S3 objects carry no permissions, so OmniFile gives every folder and file this source
            brings in these AD groups. All three are needed.
          </p>
          <GroupField
            title="Owners"
            hint="Read and write; they set rewind and the other groups."
            group={d.owners}
            onChange={(g) => set({ owners: g })}
          />
          <GroupField
            title="Writers"
            hint="Read and write; restore old versions."
            group={d.writers}
            onChange={(g) => set({ writers: g })}
          />
          <GroupField
            title="Readers"
            hint="Read, recall ahead of time, rewind and download."
            group={d.readers}
            onChange={(g) => set({ readers: g })}
          />
        </Question>
      ) : null}

      {step === "how" ? (
        <Question key="how" n={4} title="How should it move?">
          <HowFields how={how} onChange={setHow} />
          <More>
            <fieldset className="options">
              <legend className="field-label">Mode</legend>
              <label className="option">
                <input type="radio" name="mode" checked readOnly />
                <span className="option-title">Serve from Azure behind the same share</span>
                <span className="option-body">
                  The only mode in this version: people keep using the OmniFile share.
                </span>
              </label>
              <label className="option">
                <input type="radio" name="mode" disabled />
                <span className="option-title">
                  Hand off <span className="badge badge-outline">Not offered yet</span>
                </span>
                <span className="option-body">
                  Copy, verify, then let the new storage serve users directly.
                </span>
              </label>
            </fieldset>
          </More>
        </Question>
      ) : null}

      {step === "review" ? (
        <Question key="review" n={5} title="Check, then start">
          <dl className="kv">
            <dt>From</dt>
            <dd className="mono">
              s3://{bucket}/{d.prefix}
              <span className="muted">
                {" "}
                ({backend?.name}
                {backend?.region ? `, ${backend.region}` : ""})
              </span>
            </dd>
            <ScanSummary scanId={scanId} />
            <dt>Into</dt>
            <dd>
              {d.destination === "new" ? `A new dataset, ${destName}` : destName}, at{" "}
              <code>/{folderPath(d.folder)}</code>, stored on {destBackend}
            </dd>
            <dt>Access</dt>
            <dd>
              Owners {groupName(d.owners)}, Writers {groupName(d.writers)}, Readers{" "}
              {groupName(d.readers)}
            </dd>
            <dt>Copied by</dt>
            <dd>{copierLabel(how.copier === "external" ? "external" : "storage_mover")}</dd>
            <dt>Switch-over</dt>
            <dd>
              {how.cutover === "auto"
                ? "Automatically, once every file is verified"
                : "When an ArchiveAdmin presses Cut over, once every file is verified"}
            </dd>
          </dl>
          <h3>What happens</h3>
          <ol className="stack" style={{ paddingLeft: 20, margin: 0 }}>
            <li>
              OmniFile catalogues the objects: each becomes a file of the dataset, readable through
              its shares straight away and served from S3 until it moves. The counts appear as it
              goes.
            </li>
            <li>
              {how.copier === "external"
                ? "Your external copier copies each object to the target path and reports its runs through the API."
                : "Azure Storage Mover copies the objects into Azure. Archived objects are restored in waves first."}
            </li>
            <li>
              OmniFile checks every copy against S3&apos;s checksum. Nothing switches until every
              file matches; then{" "}
              {how.cutover === "auto" ? "it switches by itself." : "it waits for you."}
            </li>
          </ol>
          <Callout tone="warn" title="AWS bills some of this">
            <p>
              Objects in Glacier Flexible Retrieval or Deep Archive are restored before they are
              copied. AWS charges for each restore and bills the temporary copy on top. Reading from
              S3, by the copy and by people who open files before they move, is billed by AWS as
              data transfer out.
            </p>
            <p className="muted">
              The service has no S3 price table yet, so the portal can&apos;t estimate these.
            </p>
          </Callout>
          {d.destination === "new" && !made ? (
            <Callout tone="info">
              The new dataset has no share yet. Add one on the <Link to="/shares">Shares</Link> page
              so people can reach the files.
            </Callout>
          ) : null}
          {addDataset.isError ? (
            <ErrorCallout error={addDataset.error} title="The dataset wasn't created" />
          ) : null}
          {register.isError ? (
            <>
              <ErrorCallout error={register.error} title="The source wasn't registered" />
              {made ? (
                <Callout tone="info">
                  The dataset {made.name} was created and is kept. Fix the source and try again: it
                  is used, not made twice.
                </Callout>
              ) : null}
            </>
          ) : null}
        </Question>
      ) : null}

      <div className="row">
        {at > 0 ? (
          <button type="button" className="btn" onClick={back} disabled={busy}>
            Back
          </button>
        ) : (
          <button type="button" className="btn" onClick={() => void navigate("/migrations")}>
            Cancel
          </button>
        )}
        <span className="muted spacer" aria-live="polite">
          {problem && (step !== "source" || backend || s3.length === 0) ? problem : ""}
        </span>
        {step === "review" ? (
          <button
            type="button"
            className="btn btn-primary"
            disabled={busy || Object.values(problems).some((p) => p !== null)}
            onClick={() => void start()}
          >
            {busy ? "Starting…" : "Start migration"}
          </button>
        ) : (
          <button
            type="button"
            className="btn btn-primary"
            disabled={problem !== null}
            onClick={next}
          >
            Next
          </button>
        )}
      </div>
    </>
  );
}
