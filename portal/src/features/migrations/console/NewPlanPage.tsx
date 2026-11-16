import { useId, useState } from "react";
import { Link, useNavigate, useSearchParams } from "react-router";
import { useDatasets, useSources } from "../../../api/queries";
import { useSession } from "../../../app/session";
import { usePageTitle } from "../../../app/usePageTitle";
import { Callout, ErrorCallout, Loading } from "../../../components/Feedback";
import { useToast } from "../../../components/Toasts";
import { formatBytes } from "../../../lib/format";
import { fromUtcInput, toUtcInput } from "./consoleLabels";
import { isArchiveAdmin } from "../../../lib/permissions";
import { sourceStateLabel } from "../labels";
import { useCreatePlan, useSplit, type MigrationPlanCreate } from "./consoleApi";

const HHMM = /^([01][0-9]|2[0-3]):[0-5][0-9]$/;

/**
 * A staged plan: split a source into waves by size or by count, then give the
 * waves a start, a spacing, an optional nightly window and a cutover mode.
 * Every wave can be moved later on the plan's schedule.
 */
export function NewPlanPage() {
  usePageTitle("New migration plan");
  const { me } = useSession();
  const admin = isArchiveAdmin(me);
  const [params] = useSearchParams();
  const sources = useSources(admin);
  const datasets = useDatasets(admin);
  const create = useCreatePlan();
  const navigate = useNavigate();
  const toast = useToast();
  const [sourceId, setSourceId] = useState(params.get("source") ?? "");
  const [by, setBy] = useState<"bytes" | "count">("bytes");
  const [waves, setWaves] = useState("4");
  const [name, setName] = useState("");
  const [start, setStart] = useState(() => {
    const tomorrow = new Date(Date.now() + 86_400_000);
    tomorrow.setUTCHours(22, 0, 0, 0);
    return toUtcInput(tomorrow.toISOString());
  });
  const [spacing, setSpacing] = useState("1");
  const [useWindow, setUseWindow] = useState(true);
  const [winStart, setWinStart] = useState("22:00");
  const [winEnd, setWinEnd] = useState("06:00");
  const [chain, setChain] = useState(true);
  const [cutover, setCutover] = useState<"auto" | "manual">("auto");
  const ids = {
    source: useId(),
    waves: useId(),
    name: useId(),
    start: useId(),
    spacing: useId(),
    ws: useId(),
    we: useId(),
    byName: useId(),
    cutName: useId(),
  };
  const n = Number(waves);
  const split = useSplit(
    sourceId && Number.isInteger(n) && n >= 1 && n <= 100
      ? { source_id: sourceId, by, waves: n }
      : null,
  );
  if (!admin) {
    return (
      <div className="page">
        <h1>New migration plan</h1>
        <ErrorCallout error={new Error("Migrations are for ArchiveAdmin.")} />
      </div>
    );
  }
  const choices = (sources.data?.items ?? []).filter((s) => s.kind === "source");
  const source = choices.find((s) => s.source_id === sourceId);
  const startIso = fromUtcInput(start);
  const gapDays = Number(spacing);
  const problem = !source
    ? "Choose a source."
    : !split.data || split.data.waves.length === 0
      ? "The source has no folders to split into waves yet."
      : !name.trim()
        ? "Name the plan."
        : !startIso
          ? "Pick the first wave's start."
          : !(gapDays >= 0 && gapDays <= 60)
            ? "Days between waves: 0 to 60."
            : useWindow && (!HHMM.test(winStart) || !HHMM.test(winEnd) || winStart === winEnd)
              ? "The window: two different HH:MM times in UTC."
              : null;

  function body(): MigrationPlanCreate | null {
    if (!source || !split.data || !startIso) return null;
    const t0 = new Date(startIso).getTime();
    return {
      dataset_id: source.dataset_id,
      name: name.trim(),
      waves: split.data.waves.map((w, i) => ({
        name: `Wave ${i + 1}`,
        prefixes: w.prefixes,
        start_at: new Date(t0 + i * gapDays * 86_400_000).toISOString(),
        window: useWindow ? { start: winStart, end: winEnd } : null,
        after: chain && i > 0 ? i : null,
        cutover,
      })),
    };
  }

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
            <span aria-current="page">New plan</span>
          </li>
        </ol>
      </nav>
      <div className="page-head">
        <div>
          <h1>New migration plan</h1>
          <p className="page-question">How should this source move, wave by wave?</p>
          <p className="lede">
            Each wave starts on time, inside its window, once the wave before it is verified.
          </p>
        </div>
      </div>
      {sources.isPending || datasets.isPending ? <Loading /> : null}
      <section className="question" aria-labelledby={`${ids.source}-q`}>
        <span className="q-num" aria-hidden="true">
          1
        </span>
        <h2 id={`${ids.source}-q`}>Which source, in how many waves?</h2>
        <div className="question-body">
          <div className="grid-2">
            <div className="field">
              <label className="field-label" htmlFor={ids.source}>
                Source
              </label>
              <select
                id={ids.source}
                value={sourceId}
                onChange={(e) => setSourceId(e.target.value)}
              >
                <option value="">Choose a source</option>
                {choices.map((s) => (
                  <option key={s.source_id} value={s.source_id}>
                    {s.container}/{s.prefix} ({sourceStateLabel(s.state)})
                  </option>
                ))}
              </select>
              {source && source.state !== "seeded" ? (
                <span className="field-hint">
                  Still being catalogued: its waves wait for that and then start by themselves.
                </span>
              ) : null}
            </div>
            <div className="field">
              <label className="field-label" htmlFor={ids.waves}>
                Waves
              </label>
              <input
                id={ids.waves}
                type="number"
                min={1}
                max={100}
                value={waves}
                onChange={(e) => setWaves(e.target.value)}
              />
            </div>
          </div>
          <fieldset className="options options-row">
            <legend className="field-label">Balance the waves by</legend>
            <label className="option">
              <input
                type="radio"
                name={ids.byName}
                checked={by === "bytes"}
                onChange={() => setBy("bytes")}
              />
              <span className="option-title">Size</span>
              <span className="option-body">About the same bytes in each wave.</span>
            </label>
            <label className="option">
              <input
                type="radio"
                name={ids.byName}
                checked={by === "count"}
                onChange={() => setBy("count")}
              />
              <span className="option-title">File count</span>
              <span className="option-body">About the same number of files in each.</span>
            </label>
          </fieldset>
          {split.isError ? <ErrorCallout error={split.error} /> : null}
          {split.data ? (
            <>
              {split.data.hidden.folders ? (
                <Callout tone="info" title="Folders you can't read">
                  {split.data.hidden.folders.toLocaleString()} folders (
                  {split.data.hidden.files.toLocaleString()} files) are in this source, but you
                  can&apos;t read them, so they aren&apos;t named here or put in a wave. Someone in
                  their groups can plan them.
                </Callout>
              ) : null}
              {split.data.loose.files ? (
                <Callout tone="warn" title="Files directly under the prefix">
                  {split.data.loose.files.toLocaleString()} files lie directly under{" "}
                  <code>{split.data.prefix || "the bucket's root"}</code>, outside any folder. They
                  can only move with the whole prefix, so this plan leaves them out; migrate them
                  with the whole source once its waves are done.
                </Callout>
              ) : null}
              <div className="table-wrap card">
                <table className="table" aria-label="Waves to make">
                  <thead>
                    <tr>
                      <th scope="col">Wave</th>
                      <th scope="col">Prefixes</th>
                      <th scope="col" className="col-num">
                        Files
                      </th>
                      <th scope="col" className="col-num">
                        Size
                      </th>
                    </tr>
                  </thead>
                  <tbody>
                    {split.data.waves.map((w, i) => (
                      <tr key={w.prefixes.join(",")}>
                        <td>{i + 1}</td>
                        <td className="mono">{w.prefixes.join(", ")}</td>
                        <td className="col-num">{w.files.toLocaleString()}</td>
                        <td className="col-num">{formatBytes(w.bytes)}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </>
          ) : null}
        </div>
      </section>

      <section className="question" aria-labelledby={`${ids.start}-q`}>
        <span className="q-num" aria-hidden="true">
          2
        </span>
        <h2 id={`${ids.start}-q`}>When?</h2>
        <div className="question-body">
          <div className="grid-2">
            <div className="field">
              <label className="field-label" htmlFor={ids.start}>
                First wave starts (UTC)
              </label>
              <input
                id={ids.start}
                type="datetime-local"
                value={start}
                onChange={(e) => setStart(e.target.value)}
              />
            </div>
            <div className="field">
              <label className="field-label" htmlFor={ids.spacing}>
                Days between wave starts
              </label>
              <input
                id={ids.spacing}
                type="number"
                min={0}
                max={60}
                value={spacing}
                onChange={(e) => setSpacing(e.target.value)}
              />
            </div>
          </div>
          <label className="check-row">
            <input type="checkbox" checked={chain} onChange={(e) => setChain(e.target.checked)} />
            <span>
              Each wave waits for the one before it to be verified
              <span className="field-hint">So a problem in one wave stops the next.</span>
            </span>
          </label>
          <details className="more">
            <summary>More settings</summary>
            <div className="more-body stack">
              <label className="check-row">
                <input
                  type="checkbox"
                  checked={useWindow}
                  onChange={(e) => setUseWindow(e.target.checked)}
                />
                <span>
                  Start and cut over only inside a daily window
                  <span className="field-hint">In UTC; a copy already running goes on.</span>
                </span>
              </label>
              {useWindow ? (
                <div className="sentence">
                  <label htmlFor={ids.ws}>From</label>
                  <input
                    id={ids.ws}
                    className="inline-num"
                    value={winStart}
                    onChange={(e) => setWinStart(e.target.value)}
                  />
                  <label htmlFor={ids.we}>to</label>
                  <input
                    id={ids.we}
                    className="inline-num"
                    value={winEnd}
                    onChange={(e) => setWinEnd(e.target.value)}
                  />
                  <span>UTC</span>
                </div>
              ) : null}
              <fieldset className="options">
                <legend className="field-label">Cutover</legend>
                <label className="option">
                  <input
                    type="radio"
                    name={ids.cutName}
                    checked={cutover === "auto"}
                    onChange={() => setCutover("auto")}
                  />
                  <span className="option-title">Automatically, in the window</span>
                  <span className="option-body">Once every file of the wave is verified.</span>
                </label>
                <label className="option">
                  <input
                    type="radio"
                    name={ids.cutName}
                    checked={cutover === "manual"}
                    onChange={() => setCutover("manual")}
                  />
                  <span className="option-title">When an ArchiveAdmin says</span>
                  <span className="option-body">Each wave waits at Verified.</span>
                </label>
              </fieldset>
            </div>
          </details>
          <div className="field">
            <label className="field-label" htmlFor={ids.name}>
              Plan name
            </label>
            <input
              id={ids.name}
              value={name}
              maxLength={120}
              onChange={(e) => setName(e.target.value)}
            />
          </div>
        </div>
      </section>
      {create.isError ? <ErrorCallout error={create.error} title="The plan wasn't made" /> : null}
      <div className="row">
        <Link className="btn" to="/migrations/plans">
          Cancel
        </Link>
        <span className="muted spacer" aria-live="polite">
          {problem ?? ""}
        </span>
        <button
          type="button"
          className="btn btn-primary"
          disabled={problem !== null || create.isPending}
          onClick={() => {
            const b = body();
            if (!b) return;
            create.mutate(b, {
              onSuccess: (p) => {
                toast("Plan made.");
                void navigate(`/migrations/plans/${p.plan_id}`);
              },
            });
          }}
        >
          {create.isPending ? "Making…" : "Make plan"}
        </button>
      </div>
    </div>
  );
}
