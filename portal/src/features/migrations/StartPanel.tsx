import { useEffect, useRef } from "react";
import { Link, useNavigate } from "react-router";
import { ApiError } from "../../api/client";
import { Callout, ErrorCallout, Loading } from "../../components/Feedback";
import { useToast } from "../../components/Toasts";
import { CheckIcon, XIcon } from "../../components/icons";
import { useRetrySource, useSource, useStartMigration } from "./api";
import { useCreatePlan } from "./console/consoleApi";
import { HowFields, type How } from "./HowFields";
import { sourceStateLabel } from "./labels";

const KEY = (sourceId: string) => `omnifile.new-migration.${sourceId}`;

/** The wizard's "how" choices, kept per source so a resumed wizard has them. */
export function saveHow(sourceId: string, how: How): void {
  try {
    window.localStorage.setItem(KEY(sourceId), JSON.stringify(how));
  } catch {
    // Storage may be unavailable (private window); the defaults are used then.
  }
}

export function loadHow(sourceId: string): How | null {
  try {
    const raw = window.localStorage.getItem(KEY(sourceId));
    if (!raw) return null;
    const v = JSON.parse(raw) as Partial<How>;
    if (
      (v.copier === "auto" || v.copier === "external") &&
      (v.cutover === "auto" || v.cutover === "manual")
    ) {
      return { copier: v.copier, cutover: v.cutover };
    }
  } catch {
    // Unreadable: use the defaults.
  }
  return null;
}

const ARMED = (sourceId: string) => `omnifile.new-migration.armed.${sourceId}`;

/**
 * The admin pressed Start in this tab: the resumed page starts the migration
 * by itself once the source is catalogued. Elsewhere (a new tab, the
 * Migrations page) it asks first.
 */
export function markArmed(sourceId: string): void {
  try {
    window.sessionStorage.setItem(ARMED(sourceId), "1");
  } catch {
    // The resumed page then asks before it starts.
  }
}

export function isArmed(sourceId: string): boolean {
  try {
    return window.sessionStorage.getItem(ARMED(sourceId)) === "1";
  } catch {
    return false;
  }
}

function forget(sourceId: string): void {
  try {
    window.localStorage.removeItem(KEY(sourceId));
    window.sessionStorage.removeItem(ARMED(sourceId));
  } catch {
    // Nothing to do.
  }
}

type Status = "done" | "current" | "failed" | "pending";

function Steps({ items }: { items: { name: string; status: Status; note: string }[] }) {
  return (
    <ol
      className="steps"
      aria-label="Starting the migration"
      style={{ gridTemplateColumns: `repeat(${items.length}, minmax(0, 1fr))` }}
    >
      {items.map((s, i) => (
        <li
          key={s.name}
          className="step"
          data-status={s.status}
          aria-current={s.status === "current" ? "step" : undefined}
        >
          <span className="step-dot" aria-hidden="true">
            {s.status === "done" ? <CheckIcon /> : s.status === "failed" ? <XIcon /> : i + 1}
          </span>
          <span>
            <span className="step-name">{s.name}</span>
            <span className="sr-only">
              {" "}
              —{" "}
              {s.status === "done"
                ? "done"
                : s.status === "current"
                  ? "in progress"
                  : s.status === "failed"
                    ? "failed"
                    : "not yet"}
            </span>
          </span>
          <span className="step-at">{s.note}</span>
        </li>
      ))}
    </ol>
  );
}

/**
 * The source is registered: wait for it to be catalogued, then start its
 * migration (POST /v1/migrations refuses a source still seeding). The source
 * is kept whatever happens here, so this panel is also how a wizard resumes.
 */
export function StartPanel({
  sourceId,
  how,
  onHow,
  armed,
  onArm,
}: {
  sourceId: string;
  how: How;
  onHow: (how: How) => void;
  /** Start as soon as the source is catalogued. */
  armed: boolean;
  onArm: (armed: boolean) => void;
}) {
  const source = useSource(sourceId);
  const start = useStartMigration();
  const retry = useRetrySource();
  const navigate = useNavigate();
  const toast = useToast();
  const asked = useRef(false);
  const planned = useRef(false);
  const plan = useCreatePlan();
  const s = source.data;

  // Still being catalogued: hand the start to the worker's plan scheduler
  // (one wave, due now), so it starts even if this page is closed. If that
  // can't be made, the page starts it itself once catalogued, as before.
  useEffect(() => {
    if (!armed || !s || planned.current) return;
    if (s.state !== "registered" && s.state !== "seeding") return;
    planned.current = true;
    plan.mutate(
      {
        dataset_id: s.dataset_id,
        name: `Migration of ${s.container}/${s.prefix}`,
        waves: [
          {
            name: "Whole source",
            prefixes: [s.prefix],
            start_at: new Date().toISOString(),
            cutover: how.cutover,
            copier: how.copier,
          },
        ],
      },
      { onSuccess: () => forget(sourceId) },
    );
  }, [armed, s, how, sourceId, plan]);

  useEffect(() => {
    if (plan.isSuccess || plan.isPending) return;
    if (!armed || !s || s.state !== "seeded" || asked.current) return;
    asked.current = true;
    start.mutate(
      { dataset_id: s.dataset_id, prefix: s.prefix, cutover: how.cutover, copier: how.copier },
      {
        onSuccess: (m) => {
          forget(sourceId);
          toast("Migration started.");
          void navigate(`/migrations/${m.migration_id}`);
        },
      },
    );
  }, [armed, s, how, sourceId, start, navigate, toast, plan.isSuccess, plan.isPending]);

  if (source.isPending) return <Loading label="Loading the source…" />;
  if (source.isError) return <ErrorCallout error={source.error} title="The source can't be read" />;
  if (!s) return null;
  if (s.kind !== "source") {
    return (
      <Callout tone="info" title="Nothing to migrate">
        This source was adopted in place: its data is already in the dataset&apos;s own backend.
      </Callout>
    );
  }

  const seeded = s.state === "seeded";
  const failed = s.state === "failed";
  const counts = s.counts;
  const catalogNote = failed
    ? "Stopped"
    : seeded
      ? `${counts.files_created.toLocaleString()} files`
      : s.state === "seeding"
        ? `${counts.objects_seen.toLocaleString()} objects so far`
        : "Waiting for the worker";
  const startStatus: Status = start.isError
    ? "failed"
    : start.isPending || start.isSuccess
      ? "current"
      : armed && seeded
        ? "current"
        : "pending";

  return (
    <div className="stack-lg">
      <Steps
        items={[
          { name: "Source registered", status: "done", note: `${s.container}/${s.prefix}` },
          {
            name: "Catalogued",
            status: seeded ? "done" : failed ? "failed" : "current",
            note: catalogNote,
          },
          {
            name: "Migration started",
            status: startStatus,
            note: armed ? "" : "When you say",
          },
        ]}
      />
      <p className="sr-only" aria-live="polite">
        {`Source ${sourceStateLabel(s.state).toLowerCase()}.`}
      </p>

      {failed ? (
        <Callout tone="danger" title="Cataloguing stopped" role="alert">
          <p>{s.last_error ?? "The worker couldn't read the bucket's objects."}</p>
          <button
            type="button"
            className="btn btn-sm"
            style={{ marginTop: 8 }}
            disabled={retry.isPending}
            onClick={() => retry.mutate(s.source_id)}
          >
            Try cataloguing again
          </button>
          {retry.isError ? <ErrorCallout error={retry.error} /> : null}
        </Callout>
      ) : !seeded ? (
        <Callout tone="info" title="OmniFile is cataloguing the bucket">
          <p>
            Every object becomes a file in the dataset, with the groups you chose. A big bucket
            takes a while. You can leave this page: the source is kept, and the Migrations page
            offers to start its migration.
          </p>
        </Callout>
      ) : null}

      {seeded ? (
        <dl className="kv">
          <dt>Objects found</dt>
          <dd>{counts.objects_seen.toLocaleString()}</dd>
          <dt>Files catalogued</dt>
          <dd>{counts.files_created.toLocaleString()}</dd>
          <dt>Folders made</dt>
          <dd>{counts.dirs_created.toLocaleString()}</dd>
          {counts.already_present ? (
            <>
              <dt>Already in the dataset</dt>
              <dd>{counts.already_present.toLocaleString()}</dd>
            </>
          ) : null}
          {counts.skipped ? (
            <>
              <dt>Skipped</dt>
              <dd>
                {counts.skipped.toLocaleString()}{" "}
                <span className="muted">(keys that can&apos;t be a file name or path)</span>
              </dd>
            </>
          ) : null}
        </dl>
      ) : null}

      {start.isError ? (
        <>
          <ErrorCallout error={start.error} title="The migration didn't start" />
          <Callout tone="info">
            The source {s.container}/{s.prefix} is registered and kept.{" "}
            {start.error instanceof ApiError && start.error.code === "migration_conflict"
              ? "Fix what the message says, then try again."
              : "Try again, or start it later from the Migrations page."}
          </Callout>
          <div className="row">
            <button
              type="button"
              className="btn btn-primary"
              onClick={() => {
                asked.current = false;
                start.reset();
                onArm(true);
              }}
            >
              Try again
            </button>
            <Link className="btn" to="/migrations">
              Back to Migrations
            </Link>
          </div>
        </>
      ) : !armed ? (
        <div className="stack">
          <HowFields how={how} onChange={onHow} />
          <div className="row">
            <button
              type="button"
              className="btn btn-primary"
              disabled={failed}
              onClick={() => {
                saveHow(sourceId, how);
                onArm(true);
              }}
            >
              {seeded ? "Start migration" : "Start once catalogued"}
            </button>
            <Link className="btn" to="/migrations">
              Not now
            </Link>
          </div>
        </div>
      ) : plan.isSuccess ? (
        <Callout tone="ok" title="It starts by itself">
          OmniFile starts the migration once the source is catalogued, whether or not this page is
          open. <Link to={`/migrations/plans/${plan.data.plan_id}`}>Follow it in its plan</Link>, or
          on the Migrations page.
        </Callout>
      ) : start.isPending || start.isSuccess ? (
        <Loading label="Starting the migration…" />
      ) : !seeded && !failed ? (
        <p className="muted">
          The migration starts once the source is catalogued. Keep this page open
          {plan.isError ? " (a plan to start it without the page couldn't be made)" : ""}.
        </p>
      ) : null}
    </div>
  );
}
