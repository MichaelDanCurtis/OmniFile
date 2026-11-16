import { useId, useState } from "react";
import { Link, useParams } from "react-router";
import { PageHead } from "../../../components/PageHead";
import { MigrationTabs } from "./MigrationTabs";
import { useDatasets } from "../../../api/queries";
import { useSession } from "../../../app/session";
import { usePageTitle } from "../../../app/usePageTitle";
import { Callout, ErrorCallout, Loading } from "../../../components/Feedback";
import { useToast } from "../../../components/Toasts";
import { formatUtc, fromUtcInput, toUtcInput } from "./consoleLabels";
import { isArchiveAdmin } from "../../../lib/permissions";
import { migrationStateLabel } from "../labels";
import {
  useDecidePlan,
  usePlan,
  usePlans,
  useReschedule,
  useWaveCutover,
  type MigrationPlan,
  type MigrationWave,
} from "./consoleApi";
import { planStateLabel, waveStateLabel, windowLabel } from "./consoleLabels";
import { useWaveAction } from "./consoleApi";
import { movable, rangeOf, Schedule } from "./Schedule";

function NotAdmin({ title }: { title: string }) {
  return (
    <div className="page">
      <h1>{title}</h1>
      <ErrorCallout error={new Error("Migrations are for ArchiveAdmin.")} />
    </div>
  );
}

/** The typed alternative to dragging: a wave's start as a date and time. */
function StartForm({
  plan,
  wave,
  onSave,
  saving,
}: {
  plan: MigrationPlan;
  wave: MigrationWave;
  onSave: (seq: number, startAt: string) => void;
  saving: boolean;
}) {
  const [value, setValue] = useState(toUtcInput(wave.start_at));
  const id = useId();
  const iso = fromUtcInput(value);
  const [lo, hi] = rangeOf(plan, wave);
  const t = iso ? new Date(iso).getTime() : null;
  const problem =
    t === null
      ? "Pick a date and time."
      : lo !== null && t < lo
        ? `It waits for wave ${wave.after}, which starts ${formatUtc(new Date(lo).toISOString())}.`
        : hi !== null && t > hi
          ? "A later wave waits for this one and starts before that. Move it first."
          : null;
  return (
    <div className="row" style={{ gap: 6 }}>
      <label className="sr-only" htmlFor={id}>
        Start of wave {wave.seq} (UTC)
      </label>
      <input
        id={id}
        type="datetime-local"
        value={value}
        onChange={(e) => setValue(e.target.value)}
        aria-invalid={problem !== null}
      />
      <button
        type="button"
        className="btn btn-sm"
        disabled={saving || problem !== null || iso === wave.start_at}
        onClick={() => iso && onSave(wave.seq, iso)}
      >
        Reschedule
      </button>
      {problem ? <span className="field-error">{problem}</span> : null}
    </div>
  );
}

export function PlanPage() {
  const { planId } = useParams();
  const { me } = useSession();
  const admin = isArchiveAdmin(me);
  const plan = usePlan(admin ? planId : undefined);
  const datasets = useDatasets(admin);
  const decide = useDecidePlan(planId ?? "");
  const reschedule = useReschedule(planId ?? "");
  const cutover = useWaveCutover(planId ?? "");
  const waveAction = useWaveAction(planId ?? "");
  const toast = useToast();
  usePageTitle(plan.data ? `Plan: ${plan.data.name}` : "Migration plan");
  if (!admin) return <NotAdmin title="Migration plan" />;
  if (plan.isPending) return <Loading />;
  if (plan.isError) {
    return (
      <div className="page">
        <ErrorCallout error={plan.error} />
      </div>
    );
  }
  const p = plan.data;
  const mine = p.proposed_by === `user:${me.identity_id}`;
  const save = (seq: number, startAt: string) =>
    reschedule.mutate(
      { seq, patch: { base_version: p.version, start_at: startAt } },
      { onSuccess: () => toast(`Wave ${seq} rescheduled.`) },
    );
  const dataset = datasets.data?.items.find((d) => d.dataset_id === p.dataset_id)?.name;
  return (
    <div className="page">
      <nav aria-label="Breadcrumb">
        <ol className="crumbs">
          <li>
            <Link to="/migrations">Migrations</Link>
          </li>
          <li>
            <Link to="/migrations/plans">Plans</Link>
          </li>
          <li>
            <span aria-current="page">{p.name}</span>
          </li>
        </ol>
      </nav>
      <div className="page-head">
        <div>
          <h1>{p.name}</h1>
          <p className="page-question">When does each wave run, and what holds it back?</p>
          <p className="lede">
            {planStateLabel(p.state)} · {dataset ?? `dataset ${p.dataset_id}`} · owner{" "}
            {p.owner.display_name ?? `identity ${p.owner.identity_id}`}
            {p.proposed_by.startsWith("app:") ? " · proposed through the API" : ""}
          </p>
        </div>
        <div className="actions">
          {p.state === "proposed" && !mine ? (
            <>
              <button
                type="button"
                className="btn btn-primary"
                disabled={decide.isPending}
                onClick={() =>
                  decide.mutate(
                    { decision: "approve" },
                    { onSuccess: () => toast("Plan approved.") },
                  )
                }
              >
                Approve
              </button>
              <button
                type="button"
                className="btn"
                disabled={decide.isPending}
                onClick={() =>
                  decide.mutate(
                    { decision: "reject" },
                    { onSuccess: () => toast("Plan rejected.") },
                  )
                }
              >
                Reject
              </button>
            </>
          ) : null}
          {p.state === "proposed" || p.state === "approved" ? (
            <button
              type="button"
              className="btn btn-danger"
              disabled={decide.isPending}
              onClick={() =>
                decide.mutate(
                  { decision: "cancel" },
                  { onSuccess: () => toast("Waves not started are cancelled.") },
                )
              }
            >
              Cancel plan
            </button>
          ) : null}
        </div>
      </div>
      {decide.isError ? <ErrorCallout error={decide.error} /> : null}
      {reschedule.isError ? (
        <ErrorCallout error={reschedule.error} title="The wave wasn't rescheduled" />
      ) : null}
      {cutover.isError ? <ErrorCallout error={cutover.error} /> : null}
      {waveAction.isError ? <ErrorCallout error={waveAction.error} /> : null}
      {p.state === "proposed" ? (
        <Callout tone="info" title="Waiting for an ArchiveAdmin">
          {mine
            ? "You proposed it: another ArchiveAdmin approves it."
            : "Nothing starts until it is approved."}
        </Callout>
      ) : null}

      <section className="card card-pad stack" aria-labelledby="plan-sched">
        <h2 id="plan-sched">Schedule</h2>
        <Schedule plan={p} onSave={save} saving={reschedule.isPending} />
      </section>

      <section aria-labelledby="plan-waves" className="stack">
        <h2 id="plan-waves">Waves</h2>
        <p className="muted">
          The start time, window, blackouts and dependencies are enforced, for starting and for
          cutting over. Bandwidth and egress figures are advisory: recorded, not applied. A copy
          already running isn&apos;t paused outside its window.
        </p>
        <div className="table-wrap card">
          <table className="table table-dense" aria-label="Waves">
            <thead>
              <tr>
                <th scope="col">Wave</th>
                <th scope="col">Prefixes</th>
                <th scope="col">State</th>
                <th scope="col">Start</th>
                <th scope="col">Window</th>
                <th scope="col">Cutover</th>
                <th scope="col">Advisory caps</th>
              </tr>
            </thead>
            <tbody>
              {p.waves.map((w) => (
                <tr key={w.seq}>
                  <td>
                    {w.seq}. {w.name}
                    {w.after ? <div className="muted">after wave {w.after} is verified</div> : null}
                  </td>
                  <td className="mono">
                    {w.prefixes.map((x) => {
                      const m = w.migrations.find((y) => y.prefix === x);
                      return (
                        <div key={x}>
                          {m ? <Link to={`/migrations/${m.migration_id}`}>{x}</Link> : x}
                          {m ? (
                            <span className="muted"> · {migrationStateLabel(m.state)}</span>
                          ) : null}
                        </div>
                      );
                    })}
                  </td>
                  <td>
                    {waveStateLabel(w.state)}
                    {w.waiting_reason ? <div className="muted">{w.waiting_reason}</div> : null}
                    {w.state === "blocked" && w.after ? (
                      <div>
                        <button
                          type="button"
                          className="btn btn-sm"
                          disabled={reschedule.isPending}
                          onClick={() =>
                            reschedule.mutate(
                              { seq: w.seq, patch: { base_version: p.version, after: null } },
                              { onSuccess: () => toast(`Wave ${w.seq} no longer waits.`) },
                            )
                          }
                        >
                          Don&apos;t wait for wave {w.after}
                        </button>
                      </div>
                    ) : null}
                    {w.state === "failed" && w.migrations.length ? (
                      <div>
                        <button
                          type="button"
                          className="btn btn-sm"
                          disabled={waveAction.isPending}
                          onClick={() =>
                            waveAction.mutate(
                              { seq: w.seq, action: "retry" },
                              {
                                onSuccess: () =>
                                  toast(`Wave ${w.seq}: its failed migrations start again.`),
                              },
                            )
                          }
                        >
                          Retry wave {w.seq}
                        </button>
                      </div>
                    ) : null}
                    {w.state === "started" || w.state === "verified" || w.state === "failed" ? (
                      <div>
                        <button
                          type="button"
                          className="btn btn-sm btn-ghost"
                          disabled={waveAction.isPending}
                          onClick={() =>
                            waveAction.mutate(
                              { seq: w.seq, action: "cancel" },
                              {
                                onSuccess: () =>
                                  toast(`Wave ${w.seq} cancelled: nothing was deleted.`),
                              },
                            )
                          }
                        >
                          Cancel wave {w.seq}
                        </button>
                      </div>
                    ) : null}
                  </td>
                  <td>
                    {movable(p, w) ? (
                      <StartForm
                        key={w.start_at}
                        plan={p}
                        wave={w}
                        onSave={save}
                        saving={reschedule.isPending}
                      />
                    ) : (
                      formatUtc(w.started_at ?? w.start_at)
                    )}
                  </td>
                  <td>
                    {windowLabel(w.window)}
                    {w.blackouts.length ? (
                      <div className="muted">{w.blackouts.length} blackout(s)</div>
                    ) : null}
                  </td>
                  <td>
                    {w.cutover === "manual" ? "By an ArchiveAdmin" : "Automatic, in the window"}
                    {w.cutover === "manual" && w.state === "verified" ? (
                      <div>
                        <button
                          type="button"
                          className="btn btn-sm btn-primary"
                          disabled={cutover.isPending}
                          onClick={() =>
                            cutover.mutate(w.seq, {
                              onSuccess: () => toast(`Wave ${w.seq} cutover asked for.`),
                            })
                          }
                        >
                          Cut over wave {w.seq}
                        </button>
                      </div>
                    ) : null}
                  </td>
                  <td>
                    {w.bandwidth_mbps ? `${w.bandwidth_mbps} Mbit/s` : "—"}
                    {w.egress_gb_per_day ? <div>{w.egress_gb_per_day} GB/day egress</div> : null}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </section>
    </div>
  );
}

export function PlansPage() {
  usePageTitle("Migration plans");
  const { me } = useSession();
  const admin = isArchiveAdmin(me);
  const plans = usePlans(admin);
  const datasets = useDatasets(admin);
  if (!admin) return <NotAdmin title="Migration plans" />;
  const name = (id: string) =>
    datasets.data?.items.find((d) => d.dataset_id === id)?.name ?? `dataset ${id}`;
  return (
    <div className="page">
      <PageHead
        title="Migration plans"
        question="Which waves run when, and which wait for approval?"
        lede="A plan from an API client waits for an ArchiveAdmin's approval."
        actions={
          <Link className="btn btn-primary" to="/migrations/plans/new">
            New plan
          </Link>
        }
      />
      <MigrationTabs />
      {plans.isError ? <ErrorCallout error={plans.error} /> : null}
      {!plans.data ? (
        plans.isError ? null : (
          <Loading />
        )
      ) : plans.data.items.length === 0 ? (
        <p className="muted">No plans yet.</p>
      ) : (
        <div className="table-wrap card">
          <table className="table" aria-label="Plans">
            <thead>
              <tr>
                <th scope="col">Plan</th>
                <th scope="col">Dataset</th>
                <th scope="col">State</th>
                <th scope="col">Waves</th>
                <th scope="col">Next start</th>
              </tr>
            </thead>
            <tbody>
              {plans.data.items.map((p) => {
                const next = p.waves.find((w) => w.state === "planned" || w.state === "waiting");
                return (
                  <tr key={p.plan_id}>
                    <td>
                      <Link to={`/migrations/plans/${p.plan_id}`}>{p.name}</Link>
                      {p.proposed_by.startsWith("app:") ? (
                        <div className="muted">from the API</div>
                      ) : null}
                    </td>
                    <td>{name(p.dataset_id)}</td>
                    <td>{planStateLabel(p.state)}</td>
                    <td>
                      {p.waves.filter((w) => w.state === "cut_over").length} of {p.waves.length} cut
                      over
                    </td>
                    <td>{next ? formatUtc(next.start_at) : "—"}</td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}
