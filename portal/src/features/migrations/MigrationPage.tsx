import { useId, useState } from "react";
import { Link, useParams, useSearchParams } from "react-router";
import { useBackends, useDatasets } from "../../api/queries";
import { useSession } from "../../app/session";
import { usePageTitle } from "../../app/usePageTitle";
import { Callout, ErrorCallout, Loading } from "../../components/Feedback";
import { CheckIcon, XIcon } from "../../components/icons";
import { Modal } from "../../components/Modal";
import { useToast } from "../../components/Toasts";
import { formatDateTime } from "../../lib/format";
import { isArchiveAdmin } from "../../lib/permissions";
import {
  useExclude,
  useMigration,
  useMigrationAction,
  useStuckCopies,
  useTombstones,
  type Migration,
  type StuckKind,
} from "./api";
import { stuckNotes } from "./console/consoleLabels";
import {
  actorLabel,
  copierLabel,
  cutoverLabel,
  LIFECYCLE,
  lifecycle,
  migrationStateLabel,
  modeLabel,
  proofLabel,
  runStateLabel,
} from "./labels";

const ENDED = new Set(["cut_over", "cancelled"]);

function Lifecycle({ m }: { m: Migration }) {
  const statuses = lifecycle(m);
  const at: Record<string, string | null> = {
    verified: m.verified_at,
    cut_over: m.cut_over_at,
    pending: m.created_at,
  };
  return (
    <ol
      className="steps"
      aria-label="Migration progress"
      style={{ gridTemplateColumns: `repeat(${LIFECYCLE.length}, minmax(0, 1fr))` }}
    >
      {LIFECYCLE.map((step, i) => {
        const status = statuses[i] ?? "pending";
        const when = at[step.state];
        return (
          <li
            key={step.state}
            className="step"
            data-status={status}
            aria-current={status === "current" ? "step" : undefined}
          >
            <span className="step-dot" aria-hidden="true">
              {status === "done" ? <CheckIcon /> : status === "failed" ? <XIcon /> : i + 1}
            </span>
            <span>
              <span className="step-name">{step.name}</span>
              <span className="sr-only">
                {" "}
                —{" "}
                {status === "done"
                  ? "done"
                  : status === "current"
                    ? "in progress"
                    : status === "failed"
                      ? m.state === "cancelled"
                        ? "cancelled here"
                        : "stopped here"
                      : "not yet"}
              </span>
            </span>
            <span className="step-at">
              {status === "done" && when
                ? formatDateTime(when)
                : status === "current"
                  ? step.hint
                  : ""}
            </span>
          </li>
        );
      })}
    </ol>
  );
}

function CancelDialog({ open, onClose, m }: { open: boolean; onClose: () => void; m: Migration }) {
  const action = useMigrationAction(m.migration_id);
  const toast = useToast();
  const close = () => {
    action.reset();
    onClose();
  };
  return (
    <Modal
      open={open}
      onClose={close}
      title="Cancel this migration?"
      description="It stops moving. Nothing is deleted anywhere."
      footer={
        <>
          <button type="button" className="btn" onClick={close}>
            Keep it going
          </button>
          <button
            type="button"
            className="btn btn-danger"
            disabled={action.isPending}
            onClick={() =>
              action.mutate("cancel", {
                onSuccess: () => {
                  toast("Migration cancelled.");
                  close();
                },
              })
            }
          >
            {action.isPending ? "Cancelling…" : "Cancel migration"}
          </button>
        </>
      }
    >
      <ul className="stack" style={{ paddingLeft: 20, margin: 0 }}>
        <li>Files that already switched keep their verified copy in Azure.</li>
        <li>Everything else stays served from S3, as now.</li>
        <li>A Storage Mover run already under way finishes on its own.</li>
        <li>You can start the prefix again later; its copies are found again.</li>
      </ul>
      {action.isError ? <ErrorCallout error={action.error} /> : null}
    </Modal>
  );
}

const STUCK_LABELS: Record<StuckKind, string> = {
  no_checksum: "No checksum on record",
  stranded: "No check queued",
};

/** Takes keys from the worker's list, into the text box the dialog sends. */
function StuckCopiesPicker({
  m,
  open,
  chosen,
  onChange,
}: {
  m: Migration;
  open: boolean;
  chosen: string[];
  onChange: (keys: string[]) => void;
}) {
  const selectId = useId();
  const counts: Record<StuckKind, number> = {
    no_checksum: m.stuck_copies?.no_checksum ?? 0,
    stranded: m.stuck_copies?.stranded ?? 0,
  };
  const kinds = (["no_checksum", "stranded"] as const).filter((k) => counts[k] > 0);
  const [picked, setPicked] = useState<StuckKind | null>(null);
  const kind = picked ?? kinds[0] ?? "no_checksum";
  const list = useStuckCopies(m.migration_id, kind, open && kinds.length > 0);
  if (kinds.length === 0) return null;
  const pages = list.data?.pages ?? [];
  const first = pages[0];
  const shown = pages.flatMap((p) => p.items.map((i) => i.object_key));
  const chosenSet = new Set(chosen);
  const toggle = (key: string, on: boolean) =>
    onChange(on ? [...chosen, key] : chosen.filter((k) => k !== key));
  const missing = shown.filter((k) => !chosenSet.has(k));
  return (
    <div className="field">
      <label className="field-label" htmlFor={selectId}>
        Copies holding up the cutover
      </label>
      <select id={selectId} value={kind} onChange={(e) => setPicked(e.target.value as StuckKind)}>
        {kinds.map((k) => (
          <option key={k} value={k}>
            {STUCK_LABELS[k]} ({counts[k].toLocaleString()})
          </option>
        ))}
      </select>
      {kind === "stranded" ? (
        <span className="field-hint">
          S3 has a checksum for these, so they could still be verified if their check were queued
          again. Exclude them only if they can stay on S3.
        </span>
      ) : null}
      {list.isPending ? <Loading label="Listing the objects" /> : null}
      {list.isError ? <ErrorCallout error={list.error} /> : null}
      {first && shown.length === 0 ? (
        <span className="field-hint">
          None to list. The list leaves out what is already excluded, and an account with no AD
          identity sees none.
        </span>
      ) : null}
      {shown.length > 0 ? (
        <div className="pick-list" role="group" aria-label="Objects to exclude">
          {shown.map((key) => (
            <label key={key} className="check-row">
              <input
                type="checkbox"
                checked={chosenSet.has(key)}
                onChange={(e) => toggle(key, e.target.checked)}
              />
              <code>{key}</code>
            </label>
          ))}
        </div>
      ) : null}
      {first && shown.length > 0 ? (
        <span className="field-hint">
          As the worker last looked
          {first.counted_at ? `, ${formatDateTime(first.counted_at)}` : ""}.
          {first.count > first.list_limit
            ? ` It lists the first ${first.list_limit.toLocaleString()} of ${first.count.toLocaleString()} by key; exclude those and the next ones show at its next sweep.`
            : ""}
        </span>
      ) : null}
      {shown.length > 0 ? (
        <div className="row">
          <button
            type="button"
            className="btn btn-sm"
            disabled={missing.length === 0}
            onClick={() => onChange([...chosen, ...missing])}
          >
            Choose all {shown.length.toLocaleString()} shown
          </button>
          {list.hasNextPage ? (
            <button
              type="button"
              className="btn btn-sm"
              disabled={list.isFetchingNextPage}
              onClick={() => void list.fetchNextPage()}
            >
              {list.isFetchingNextPage ? "Loading…" : "Show more"}
            </button>
          ) : null}
        </div>
      ) : null}
    </div>
  );
}

function ExcludeDialog({ open, onClose, m }: { open: boolean; onClose: () => void; m: Migration }) {
  const exclude = useExclude(m.migration_id);
  const toast = useToast();
  const [keysText, setKeysText] = useState("");
  const [reason, setReason] = useState("");
  const keysId = useId();
  const keysHint = useId();
  const reasonId = useId();
  const keys = keysText
    .split("\n")
    .map((k) => k.trim())
    .filter(Boolean);
  const outside = keys.filter((k) => !k.startsWith(m.prefix));
  const problem =
    keys.length === 0
      ? "List at least one object key."
      : keys.length > 1000
        ? "At most 1,000 keys at a time."
        : keys.some((k) => k.length > 1024)
          ? "A key is at most 1024 characters."
          : reason.trim() === ""
            ? "Say why these objects can't be verified."
            : reason.length > 500
              ? "The reason is at most 500 characters."
              : null;
  const close = () => {
    exclude.reset();
    setKeysText("");
    setReason("");
    onClose();
  };
  return (
    <Modal
      open={open}
      onClose={close}
      wide
      title="Exclude objects from verification"
      description="For objects that can never be verified, such as SSE-C objects S3 can't checksum."
      footer={
        <>
          <span className="muted spacer" aria-live="polite">
            {keysText || reason ? problem : ""}
          </span>
          <button type="button" className="btn" onClick={close}>
            Cancel
          </button>
          <button
            type="button"
            className="btn btn-danger"
            disabled={problem !== null || exclude.isPending}
            onClick={() =>
              exclude.mutate(
                { object_keys: keys, reason: reason.trim() },
                {
                  onSuccess: (next) => {
                    toast(`${next.files.excluded} objects are now excluded.`);
                    close();
                  },
                },
              )
            }
          >
            {exclude.isPending ? "Excluding…" : "Exclude them"}
          </button>
        </>
      }
    >
      <Callout tone="warn" title="This lowers the bar">
        Excluded objects are never switched: they stay served from S3, and the dataset never
        finishes migrating while they exist. It is recorded in the audit log, with your reason. You
        need to have signed in within the last 10 minutes.
      </Callout>
      <StuckCopiesPicker
        m={m}
        open={open}
        chosen={keys}
        onChange={(next) => setKeysText(next.join("\n"))}
      />
      <div className="field">
        <label className="field-label" htmlFor={keysId}>
          Object keys
        </label>
        <textarea
          id={keysId}
          className="mono"
          rows={6}
          value={keysText}
          placeholder={`${m.prefix}line_1042.segy`}
          aria-describedby={keysHint}
          onChange={(e) => setKeysText(e.target.value)}
        />
        <span id={keysHint} className="field-hint">
          One S3 key per line, under <code>{m.prefix || "(the whole source)"}</code>. Only live
          files without a verified copy are excluded; other keys are ignored.
        </span>
        {outside.length > 0 ? (
          <span className="field-error">
            {outside.length} {outside.length === 1 ? "key is" : "keys are"} outside the prefix and
            will be ignored.
          </span>
        ) : null}
      </div>
      <div className="field">
        <label className="field-label" htmlFor={reasonId}>
          Why
        </label>
        <input
          id={reasonId}
          type="text"
          value={reason}
          maxLength={500}
          placeholder="SSE-C: S3 can't give a checksum"
          onChange={(e) => setReason(e.target.value)}
        />
      </div>
      {exclude.isError ? <ErrorCallout error={exclude.error} /> : null}
    </Modal>
  );
}

function CutoverReport({ m }: { m: Migration }) {
  const report = useTombstones(m.migration_id, m.source_deletes_pending > 0);
  const total = m.source_deletes_pending;
  return (
    <section className="card card-pad stack" aria-labelledby="mig-report">
      <h2 id="mig-report">Cutover report</h2>
      <p>
        Files deleted through OmniFile while their bytes were still in S3. OmniFile never deletes
        anything in the bucket: whoever cleans it up uses this list.
      </p>
      {total === 0 ? (
        <p className="muted">No deletes are waiting in the source.</p>
      ) : report.isPending ? (
        <Loading />
      ) : report.isError ? (
        <ErrorCallout error={report.error} />
      ) : (
        <>
          <p>
            <strong>
              {total.toLocaleString()} {total === 1 ? "delete is" : "deletes are"} waiting in the
              source.
            </strong>
          </p>
          <p className="muted">
            The full list (S3 keys and paths, never file contents) is readable by ArchiveAdmins and
            by registered API clients with Archive.Adopt, even for files they can&apos;t otherwise
            read. An API client never reads another client&apos;s migration. Every read is recorded
            in the audit log.
          </p>
          {/* Only a whole report can be compared with the total. */}
          {!report.data.next_cursor && report.data.items.length < total ? (
            <Callout tone="info">
              {report.data.items.length === 0
                ? "Your account has no AD identity, so the list isn't shown to you."
                : "The list changed since the page loaded. Reload to see all of it."}
            </Callout>
          ) : null}
          {report.data.items.length > 0 ? (
            <div className="table-wrap">
              <table className="table" aria-label="Deletes waiting in the source">
                <thead>
                  <tr>
                    <th scope="col">File</th>
                    <th scope="col">S3 object</th>
                    <th scope="col">Deleted</th>
                    <th scope="col">Who</th>
                  </tr>
                </thead>
                <tbody>
                  {report.data.items.map((t) => (
                    <tr key={`${t.file_id}-${t.source.key}`}>
                      <td className="mono">{t.path}</td>
                      <td className="mono">
                        {t.source.key}
                        {t.source.backend_version_id ? (
                          <div className="muted">version {t.source.backend_version_id}</div>
                        ) : t.source.verify_etag_before_delete ? (
                          <div className="muted">unversioned: check the ETag before deleting</div>
                        ) : null}
                      </td>
                      <td>{formatDateTime(t.deleted_at)}</td>
                      <td>
                        {t.deleted_by.identity_id
                          ? `identity ${t.deleted_by.identity_id}`
                          : "not known"}
                        <div className="muted">
                          {t.deleted_by.edge_id ? `through edge ${t.deleted_by.edge_id}, ` : ""}
                          {proofLabel(t.deleted_by.proof)}
                        </div>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          ) : null}
          {report.data.next_cursor ? <p className="muted">The first 100 rows are shown.</p> : null}
        </>
      )}
    </section>
  );
}

/** One migration: its lifecycle, counts, copier, actions and cutover report. */
export function MigrationPage() {
  const { migrationId } = useParams();
  const { me } = useSession();
  const admin = isArchiveAdmin(me);
  const migration = useMigration(admin ? migrationId : undefined);
  const datasets = useDatasets(admin);
  const backends = useBackends(admin);
  const action = useMigrationAction(migrationId ?? "");
  const toast = useToast();
  const [cancelling, setCancelling] = useState(false);
  // The console's "Exclude objects…" fix opens the dialog (re-auth applies there).
  const [search] = useSearchParams();
  const [excluding, setExcluding] = useState(search.get("exclude") === "1");
  const m = migration.data;
  const dataset = datasets.data?.items.find((x) => x.dataset_id === m?.dataset_id);
  usePageTitle(m ? `Migration of ${m.prefix || "a whole source"}` : "Migration");

  if (!admin) {
    return (
      <div className="page">
        <h1>Migration</h1>
        <ErrorCallout error={new Error("Migrations are for ArchiveAdmin.")} />
      </div>
    );
  }
  if (migration.isPending) return <Loading />;
  if (migration.isError || !m) {
    return (
      <div className="page">
        <ErrorCallout error={migration.error} />
      </div>
    );
  }
  const backendName = (id: string) =>
    backends.data?.backends.find((b) => b.backend_id === id)?.name ?? `backend ${id}`;
  const total = m.files.total ?? 0;
  const manual = m.cutover === "manual";
  const canCutOver = manual && m.state === "verified" && !m.cutover_requested_at;
  const run = m.copier.last_run;

  return (
    <div className="page">
      <nav aria-label="Breadcrumb">
        <ol className="crumbs">
          <li>
            <Link to="/migrations">Migrations</Link>
          </li>
          <li>
            <span aria-current="page">{m.prefix || "(whole source)"}</span>
          </li>
        </ol>
      </nav>
      <div className="page-head">
        <div>
          <h1>
            {dataset?.name ?? `Dataset ${m.dataset_id}`}:{" "}
            <span className="mono">{m.prefix || "(whole source)"}</span>
          </h1>
          <p className="page-question">
            Where is this prefix in its move, and is it safe to cut over?
          </p>
          <p className="lede">
            {migrationStateLabel(m.state)} · from {backendName(m.from_backend_id)} to{" "}
            {backendName(m.to_backend_id)}
          </p>
        </div>
        <div className="actions">
          {m.plan ? (
            <Link className="btn" to={`/migrations/plans/${m.plan.plan_id}`}>
              Open its plan
            </Link>
          ) : null}
          {!m.plan && manual && !ENDED.has(m.state) && m.state !== "failed" ? (
            <button
              type="button"
              className="btn btn-primary"
              disabled={!canCutOver || action.isPending}
              title={
                canCutOver
                  ? undefined
                  : m.cutover_requested_at
                    ? "Already asked"
                    : "Once every file is verified"
              }
              onClick={() =>
                action.mutate("cutover", { onSuccess: () => toast("Cutover asked for.") })
              }
            >
              Cut over
            </button>
          ) : null}
          {!m.plan && m.state === "failed" ? (
            <button
              type="button"
              className="btn btn-primary"
              disabled={action.isPending}
              onClick={() =>
                action.mutate("retry", { onSuccess: () => toast("Migration started again.") })
              }
            >
              Retry
            </button>
          ) : null}
          {!ENDED.has(m.state) ? (
            <>
              <button type="button" className="btn" onClick={() => setExcluding(true)}>
                Exclude objects
              </button>
              {m.plan ? null : (
                <button
                  type="button"
                  className="btn btn-danger"
                  onClick={() => setCancelling(true)}
                >
                  Cancel
                </button>
              )}
            </>
          ) : null}
        </div>
      </div>
      {action.isError ? <ErrorCallout error={action.error} /> : null}
      {m.plan ? (
        <Callout tone="info" title={`Wave ${m.plan.seq} of a staged plan`}>
          The plan owns this migration: cut it over, cancel or retry it on{" "}
          <Link to={`/migrations/plans/${m.plan.plan_id}`}>its plan</Link>, where the wave&apos;s
          window and blackouts apply.
        </Callout>
      ) : null}

      <div className="card card-pad stack-lg">
        <Lifecycle m={m} />
        {m.state === "failed" ? (
          <Callout tone="danger" title="The migration stopped" role="alert">
            <p>{m.last_error ?? "The worker couldn't go on."}</p>
            <p>
              Retry starts it again from Pending: a new run copies only what is missing, and copies
              already verified stay verified.
            </p>
          </Callout>
        ) : m.state === "cancelled" ? (
          <Callout tone="info" title={`Cancelled ${formatDateTime(m.cancelled_at)}`}>
            Nothing was deleted. Files that switched keep their Azure copy; the rest are served from
            S3. Start the prefix again with New migration to pick it up.
          </Callout>
        ) : m.state === "cut_over" ? (
          <Callout tone="ok" title="Done">
            Every file under the prefix is served from Azure. The S3 objects stay as secondary
            copies; OmniFile never deletes them.
          </Callout>
        ) : manual && m.state === "verified" ? (
          m.cutover_requested_at ? (
            <Callout tone="info" title="Cutover asked for">
              Asked {formatDateTime(m.cutover_requested_at)}. The worker switches the files over in
              batches.
            </Callout>
          ) : (
            <Callout tone="ok" title="Every file is verified">
              Press Cut over to serve the prefix from Azure. Until then people read from S3.
            </Callout>
          )
        ) : m.last_error ? (
          <Callout tone="warn" title="Waiting on something">
            {m.last_error}
          </Callout>
        ) : null}
        {stuckNotes(m.stuck_copies).map((note) => (
          <Callout key={note} tone="warn" title="Copies are stuck">
            {note}
          </Callout>
        ))}

        <section className="stack" aria-labelledby="mig-files">
          <h2 id="mig-files">Files</h2>
          <div>
            <progress
              max={Math.max(1, total)}
              value={m.files.verified}
              aria-label={`${m.files.verified} of ${total} files verified`}
              style={{ width: "100%" }}
            />
          </div>
          <dl className="kv">
            <dt>Files under the prefix</dt>
            <dd>{m.files.total === null ? "Not counted yet" : m.files.total.toLocaleString()}</dd>
            <dt>Verified</dt>
            <dd>{m.files.verified.toLocaleString()}</dd>
            <dt>Copies that failed the check</dt>
            <dd>
              {m.files.failed.toLocaleString()}
              {m.files.failed ? (
                <span className="muted"> (removed, and copied again by the next run)</span>
              ) : null}
            </dd>
            <dt>Excluded</dt>
            <dd>
              {m.files.excluded.toLocaleString()}
              {m.files.excluded ? <span className="muted"> (stay served from S3)</span> : null}
            </dd>
          </dl>
        </section>
      </div>

      <div className="grid-2">
        <section className="card card-pad stack" aria-labelledby="mig-copier">
          <h2 id="mig-copier">Copier</h2>
          <dl className="kv">
            <dt>Copied by</dt>
            <dd>{copierLabel(m.copier_kind)}</dd>
            {m.copier.job_ref ? (
              <>
                <dt>Storage Mover job</dt>
                <dd className="mono">{m.copier.job_ref}</dd>
              </>
            ) : null}
            <dt>Last run</dt>
            <dd>
              {run ? (
                <>
                  <span className="mono">{run.run_ref}</span> · {runStateLabel(run.state)}
                  {run.started_at ? ` · started ${formatDateTime(run.started_at)}` : ""}
                  {Object.keys(run.counters).length > 0 ? (
                    <div className="muted">
                      {Object.entries(run.counters)
                        .map(([k, v]) => `${k.replace(/_/g, " ")} ${v.toLocaleString()}`)
                        .join(", ")}
                    </div>
                  ) : null}
                </>
              ) : (
                "None yet"
              )}
            </dd>
            {m.copier_kind !== "storage_mover" ? (
              <>
                <dt>Runs reported</dt>
                <dd>{m.copier.runs_reported}</dd>
              </>
            ) : null}
            <dt>Copies land at</dt>
            <dd className="mono">
              {m.to_container}/{m.target_root}
            </dd>
          </dl>
          <p className="muted">
            The copier&apos;s counts are for display. OmniFile verifies every file itself.
          </p>
        </section>
        <section className="card card-pad stack" aria-labelledby="mig-details">
          <h2 id="mig-details">Details</h2>
          <dl className="kv">
            <dt>Switch-over</dt>
            <dd>{cutoverLabel(m.cutover)}</dd>
            <dt>Mode</dt>
            <dd>{modeLabel(m.mode)}</dd>
            <dt>Started by</dt>
            <dd>{actorLabel(m.created_by)}</dd>
            <dt>Started</dt>
            <dd>{formatDateTime(m.created_at)}</dd>
            <dt>Verified</dt>
            <dd>{formatDateTime(m.verified_at)}</dd>
            <dt>Bucket events applied to</dt>
            <dd>{formatDateTime(m.journal_checked_through)}</dd>
            <dt>Cut over</dt>
            <dd>{formatDateTime(m.cut_over_at)}</dd>
          </dl>
        </section>
      </div>

      <CutoverReport m={m} />
      <CancelDialog open={cancelling} onClose={() => setCancelling(false)} m={m} />
      <ExcludeDialog open={excluding} onClose={() => setExcluding(false)} m={m} />
    </div>
  );
}
