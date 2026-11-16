import { useId, useState } from "react";
import { usePageTitle } from "../../app/usePageTitle";
import { Callout, ErrorCallout, Loading } from "../../components/Feedback";
import { Modal } from "../../components/Modal";
import { useToast } from "../../components/Toasts";
import { formatDateTime } from "../../lib/format";
import { CheckList } from "./common";
import {
  useBackendChecks,
  useSaveBackend,
  useStorageSettings,
  type BackendDraft,
  type StorageBackend,
} from "./queries";

const KIND: Record<string, string> = {
  azure_blob: "Azure Blob",
  s3: "Amazon S3",
  sim: "Simulator",
};

function Checks({ backend }: { backend: StorageBackend }) {
  const [run, setRun] = useState(false);
  const checks = useBackendChecks(backend.backend_id, run);
  return (
    <div className="stack">
      <button
        type="button"
        className="btn btn-sm"
        aria-label={`Run checks for ${backend.name}`}
        disabled={checks.isFetching}
        onClick={() => (run ? void checks.refetch() : setRun(true))}
      >
        {checks.isFetching ? "Checking…" : run ? "Check again" : "Run checks"}
      </button>
      {checks.isError ? <ErrorCallout error={checks.error} title="The checks didn't run" /> : null}
      {checks.data ? (
        <>
          <p className="muted">Checked {formatDateTime(checks.data.checked_at)}</p>
          <CheckList checks={checks.data.checks} label={`Checks for ${backend.name}`} />
        </>
      ) : null}
    </div>
  );
}

export function StoragePage() {
  usePageTitle("Settings: storage");
  const storage = useStorageSettings();
  const [editing, setEditing] = useState<StorageBackend | "new" | null>(null);
  if (storage.isPending) return <Loading />;
  if (storage.isError) return <ErrorCallout error={storage.error} />;
  const s = storage.data;
  return (
    <section className="stack" aria-labelledby="storage-h">
      <div className="page-head">
        <div>
          <h2 id="storage-h">Storage</h2>
          <p className="lede">
            Where files are kept. Azure is reached with the service&apos;s managed identity and its
            RBAC role, and S3 by federating that identity to an AWS role. No storage keys are
            stored.
          </p>
        </div>
        <button type="button" className="btn btn-primary" onClick={() => setEditing("new")}>
          Add backend
        </button>
      </div>
      {s.private_network ? (
        <Callout title="Private networking is on">
          The service reaches storage only through private endpoints, and never falls back to a
          public path (decision 22). A backend can&apos;t be made public.
        </Callout>
      ) : null}
      {s.backends.length === 0 ? (
        <p className="card card-pad">No backends yet. Add the live and archive storage accounts.</p>
      ) : (
        s.backends.map((b) => (
          <article key={b.backend_id} className="card card-pad stack" aria-label={b.name}>
            <div className="row" style={{ justifyContent: "space-between" }}>
              <div>
                <h3>{b.name}</h3>
                <p>
                  <span className="badge badge-outline">{KIND[b.kind] ?? b.kind}</span>{" "}
                  {b.private_network ? (
                    <span className="badge badge-ok">Private endpoints</span>
                  ) : (
                    <span className="badge badge-warn">Public (development only)</span>
                  )}{" "}
                  {b.can_be_home ? null : (
                    <span className="badge badge-info">Migration source only</span>
                  )}
                </p>
                <p>{b.where}</p>
                <p className="muted">
                  Used by {b.uses.datasets} datasets, {b.uses.sources} sources and{" "}
                  {b.uses.migrations} migrations.
                </p>
              </div>
              {b.kind === "azure_blob" || b.kind === "s3" ? (
                <button
                  type="button"
                  className="btn btn-sm"
                  aria-label={`Edit ${b.name}`}
                  onClick={() => setEditing(b)}
                >
                  Edit
                </button>
              ) : null}
            </div>
            <Checks backend={b} />
          </article>
        ))
      )}
      <BackendDialog
        target={editing}
        awsConfigured={s.aws_configured}
        onClose={() => setEditing(null)}
      />
    </section>
  );
}

function draftOf(b: StorageBackend | "new" | null): Form {
  const empty: Form = {
    kind: "azure",
    layout: "two",
    name: "",
    live: "",
    archive: "",
    account: "",
    worm: false,
    container: "data",
    region: "",
    awsAccount: "",
    buckets: "",
  };
  if (!b || b === "new") return empty;
  const c = b.config as Record<string, unknown>;
  const str = (v: unknown) => (typeof v === "string" ? v : "");
  if (b.kind === "s3") {
    const buckets = Array.isArray(c.buckets) ? (c.buckets as string[]).join(", ") : "";
    return {
      ...empty,
      kind: "s3",
      name: b.name,
      awsAccount: str(c.account_id),
      region: str(c.region),
      buckets,
    };
  }
  const container = str(c.container).split("/")[1] ?? "data";
  if (c.layout === "single_account") {
    return {
      ...empty,
      name: b.name,
      layout: "one",
      account: str(c.primary_account),
      worm: c.worm === true,
      container,
      region: str(c.region),
    };
  }
  return {
    ...empty,
    name: b.name,
    live: str(c.primary_account),
    archive: str(c.archive_account),
    container,
    region: str(c.region),
  };
}

interface Form {
  kind: "azure" | "s3";
  layout: "two" | "one";
  name: string;
  live: string;
  archive: string;
  account: string;
  worm: boolean;
  container: string;
  region: string;
  awsAccount: string;
  buckets: string;
}

function toDraft(f: Form): BackendDraft {
  const opt = (v: string) => (v.trim() ? v.trim() : undefined);
  if (f.kind === "s3") {
    return {
      kind: "s3",
      name: opt(f.name),
      account: f.awsAccount.trim(),
      region: f.region.trim(),
      buckets: f.buckets
        .split(/[\s,]+/)
        .map((b) => b.trim())
        .filter(Boolean),
    };
  }
  const base = {
    kind: "azure" as const,
    name: opt(f.name),
    container: opt(f.container),
    region: opt(f.region),
  };
  return f.layout === "one"
    ? { ...base, account: f.account.trim(), worm: f.worm }
    : { ...base, live: f.live.trim(), archive: f.archive.trim() };
}

function Field({
  label,
  value,
  onChange,
  hint,
  disabled,
}: {
  label: string;
  value: string;
  onChange: (v: string) => void;
  hint?: string;
  disabled?: boolean;
}) {
  const id = useId();
  const hintId = useId();
  return (
    <div className="field">
      <label className="field-label" htmlFor={id}>
        {label}
      </label>
      <input
        id={id}
        type="text"
        autoComplete="off"
        spellCheck={false}
        value={value}
        disabled={disabled}
        aria-describedby={hint ? hintId : undefined}
        onChange={(e) => onChange(e.target.value)}
      />
      {hint ? (
        <p id={hintId} className="field-hint">
          {hint}
        </p>
      ) : null}
    </div>
  );
}

export function BackendDialog({
  target,
  awsConfigured,
  onClose,
}: {
  target: StorageBackend | "new" | null;
  awsConfigured: boolean;
  onClose: () => void;
}) {
  const save = useSaveBackend();
  const toast = useToast();
  const [form, setForm] = useState<Form>(() => draftOf(target));
  const [shownFor, setShownFor] = useState(target);
  if (shownFor !== target) {
    setShownFor(target);
    setForm(draftOf(target));
  }
  const editing = target !== null && target !== "new";
  const set = <K extends keyof Form>(k: K, v: Form[K]) => setForm((f) => ({ ...f, [k]: v }));

  function close() {
    save.reset();
    onClose();
  }

  return (
    <Modal
      open={target !== null}
      onClose={close}
      wide
      title={editing ? `Edit ${target.name}` : "Add a storage backend"}
      description="Checked by the same rules as the admin command line. Once data uses a backend, only S3 buckets can be added."
      footer={
        <>
          <button type="button" className="btn" onClick={close}>
            Cancel
          </button>
          <button
            type="button"
            className="btn btn-primary"
            disabled={save.isPending}
            onClick={() =>
              save.mutate(
                { backendId: editing ? target.backend_id : undefined, draft: toDraft(form) },
                {
                  onSuccess: (r) => {
                    toast(
                      r.outcome === "unchanged"
                        ? `${r.backend.name} already has these settings.`
                        : `${r.backend.name} was ${r.outcome}.`,
                    );
                    close();
                  },
                },
              )
            }
          >
            {save.isPending ? "Saving…" : editing ? "Save" : "Add backend"}
          </button>
        </>
      }
    >
      <div className="stack">
        <fieldset className="field" disabled={editing}>
          <legend className="field-label">Provider</legend>
          <label className="row">
            <input
              type="radio"
              name="kind"
              checked={form.kind === "azure"}
              onChange={() => set("kind", "azure")}
            />
            Azure Blob
          </label>
          <label className="row">
            <input
              type="radio"
              name="kind"
              checked={form.kind === "s3"}
              onChange={() => set("kind", "s3")}
            />
            Amazon S3 (a migration source)
          </label>
        </fieldset>
        <Field
          label="Name (optional)"
          value={form.name}
          onChange={(v) => set("name", v)}
          disabled={editing}
          hint="Letters, digits, dots, dashes. Left empty, a name is chosen from the accounts."
        />
        {form.kind === "azure" ? (
          <>
            <fieldset className="field" disabled={editing}>
              <legend className="field-label">Layout</legend>
              <label className="row">
                <input
                  type="radio"
                  name="layout"
                  checked={form.layout === "two"}
                  onChange={() => set("layout", "two")}
                />
                Two accounts: live (ZRS, Smart) and archive (LRS or GRS)
              </label>
              <label className="row">
                <input
                  type="radio"
                  name="layout"
                  checked={form.layout === "one"}
                  onChange={() => set("layout", "one")}
                />
                One account, for WORM shares
              </label>
            </fieldset>
            {form.layout === "two" ? (
              <>
                <Field
                  label="Live storage account"
                  value={form.live}
                  onChange={(v) => set("live", v)}
                  hint="3 to 24 lowercase letters and digits."
                />
                <Field
                  label="Archive storage account"
                  value={form.archive}
                  onChange={(v) => set("archive", v)}
                />
              </>
            ) : (
              <>
                <Field
                  label="Storage account"
                  value={form.account}
                  onChange={(v) => set("account", v)}
                />
                <label className="row">
                  <input
                    type="checkbox"
                    checked={form.worm}
                    onChange={(e) => set("worm", e.target.checked)}
                  />
                  Version-level WORM (decision 31)
                </label>
              </>
            )}
            <Field label="Container" value={form.container} onChange={(v) => set("container", v)} />
            <Field
              label="Region (optional)"
              value={form.region}
              onChange={(v) => set("region", v)}
              hint="For example westeurope."
            />
          </>
        ) : (
          <>
            {awsConfigured ? null : (
              <Callout tone="warn">
                The service has no AWS settings yet, so an S3 backend can&apos;t be checked. See
                Settings, AWS.
              </Callout>
            )}
            <Field
              label="AWS account id"
              value={form.awsAccount}
              onChange={(v) => set("awsAccount", v)}
              disabled={editing}
            />
            <Field
              label="Region"
              value={form.region}
              onChange={(v) => set("region", v)}
              disabled={editing}
              hint="For example eu-west-1."
            />
            <Field
              label="Buckets"
              value={form.buckets}
              onChange={(v) => set("buckets", v)}
              hint="Comma-separated. Buckets can be added later, never removed once data uses them."
            />
          </>
        )}
        <p className="muted">
          Private networking stays on: storage is reached only through its private endpoints.
        </p>
        {save.isError ? <ErrorCallout error={save.error} title="The backend wasn't saved" /> : null}
      </div>
    </Modal>
  );
}
