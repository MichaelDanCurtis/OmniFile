import { useEffect, useId, useMemo, useRef, useState, type KeyboardEvent } from "react";
import { Link, useNavigate, useParams } from "react-router";
import { ApiError } from "../../api/client";
import { usePageTitle } from "../../app/usePageTitle";
import { ErrorCallout, Loading } from "../../components/Feedback";
import { formatBytes } from "../../lib/format";
import { useAnnex } from "./api";
import {
  CHECK_LABEL,
  TRIGGER_LABEL,
  excludes,
  removes,
  reportOf,
  rulesOf,
  useCreateDryRun,
  useDryRun,
  useProposeRuleset,
  useRuleset,
  useRulesets,
  countText,
  type Action,
  type Conditions,
  type Report,
  type Rule,
  type RuleBody,
  type RuleCheck,
  type Trigger,
} from "./rulesApi";
import { BackFlow, ReturnRuleRow, newReturnRule } from "./BackRules";
import {
  isReturn,
  returnTemplate,
  useReturnPreview,
  type ReturnPreview,
  type ReturnRule,
} from "./returnsApi";

const TIER: Action = { copy_to: { kind: "tier" } };
const GIB = 1024 ** 3;

/** What a rule's "Then" does, as one choice in the builder. */
type Outcome = "copy" | "exclude" | "move" | "copy_remove_after" | "remove" | "keep";

const OUTCOMES: Record<Trigger, { value: Outcome; label: string }[]> = {
  arrived: [
    { value: "copy", label: "Copy to Azure" },
    { value: "copy_remove_after", label: "Copy, then remove from the NAS after N days" },
    { value: "move", label: "Copy, then remove from the NAS" },
    { value: "exclude", label: "Leave it: stays on the NAS only" },
  ],
  changed: [
    { value: "copy", label: "Copy again, as a new version" },
    { value: "exclude", label: "Don't copy the change" },
  ],
  age: [
    { value: "remove", label: "Remove from the NAS (its copy stays in Azure)" },
    { value: "keep", label: "Keep it on the NAS" },
  ],
  deleted_at_source: [{ value: "keep", label: "Keep the copy in Azure" }],
};

function outcomeOf(r: Rule): Outcome {
  if (r.trigger === "deleted_at_source") return "keep";
  if (r.trigger === "age") return removes(r) ? "remove" : "keep";
  if (excludes(r)) return "exclude";
  if (r.actions.some((a) => "remove_from_source" in a)) return "move";
  if (r.actions.some((a) => "remove_after_days" in a)) return "copy_remove_after";
  return "copy";
}

function daysOf(r: Rule): number {
  for (const a of r.actions) if ("remove_after_days" in a) return a.remove_after_days;
  return 30;
}

function actionsFor(trigger: Trigger, o: Outcome, days: number, keep: Action[]): Action[] {
  const extras = keep.filter((a) => "tag" in a || "policy" in a);
  switch (o) {
    case "copy":
      return [TIER, ...extras];
    case "exclude":
      return [{ exclude: true }];
    case "move":
      return [TIER, { remove_from_source: true }, ...extras];
    case "copy_remove_after":
      return [TIER, { remove_after_days: days }, ...extras];
    case "remove":
      return [{ remove_from_source: true }];
    case "keep":
      return trigger === "deleted_at_source" ? [{ keep_copy: true }] : [{ exclude: true }];
  }
}

let seq = 0;
function newId(trigger: Trigger): string {
  seq += 1;
  return `${trigger}-${Date.now().toString(36)}-${seq}`;
}

/** The starting points (annex.md 4.4): the service's templates. */
export function templateRules(name: "copy" | "archive_after" | "move_now", days = 30): Rule[] {
  const base: Rule[] = [
    { id: "copy", trigger: "arrived", conditions: {}, actions: [TIER] },
    { id: "recopy", trigger: "changed", conditions: {}, actions: [TIER] },
    { id: "keep", trigger: "deleted_at_source", conditions: {}, actions: [{ keep_copy: true }] },
  ];
  if (name === "move_now") {
    base[0] = {
      id: "move",
      trigger: "arrived",
      conditions: {},
      actions: [TIER, { remove_from_source: true }],
    };
  }
  if (name !== "move_now") {
    // The default return template (annex.md 4.4): back where it came from.
    base.push(...(returnTemplate("origin") as unknown as Rule[]));
  }
  if (name === "archive_after") {
    base.splice(
      2,
      0,
      {
        id: "archive",
        trigger: "age",
        conditions: { age_since_arrival_days_gt: days },
        actions: [{ remove_from_source: true }],
      },
      { id: "stay", trigger: "age", conditions: {}, actions: [{ exclude: true }] },
    );
  }
  return base;
}

function list(v: string): string[] | undefined {
  const out = v
    .split(",")
    .map((s) => s.trim())
    .filter(Boolean);
  return out.length ? out : undefined;
}

function chips(c: Conditions): string[] {
  const out: string[] = [];
  if (c.path?.length) out.push(c.path.join(" or "));
  if (c.extension?.length) out.push(c.extension.map((e) => `.${e}`).join(", "));
  if (c.content_type?.length) out.push(c.content_type.map((t) => t.toUpperCase()).join(", "));
  if (c.size_gte !== undefined) out.push(`≥ ${formatBytes(c.size_gte)}`);
  if (c.size_lt !== undefined) out.push(`< ${formatBytes(c.size_lt)}`);
  if (c.age_since_arrival_days_gt !== undefined)
    out.push(`arrived over ${c.age_since_arrival_days_gt} days ago`);
  if (c.modified_age_days_gt !== undefined)
    out.push(`not changed for ${c.modified_age_days_gt} days`);
  if (c.accessed_age_days_gt !== undefined)
    out.push(`not opened for ${c.accessed_age_days_gt} days`);
  if (c.group_sids?.length) out.push(`group ${c.group_sids.join(", ")}`);
  if (c.owner_sids?.length) out.push(`owner ${c.owner_sids.join(", ")}`);
  return out;
}

function endsUp(r: Rule): string {
  const o = outcomeOf(r);
  if (o === "copy") return "NAS + Azure";
  if (o === "exclude") return r.trigger === "changed" ? "the old copy stays" : "stays on NAS only";
  if (o === "move") return "leaves the NAS after its copy";
  if (o === "copy_remove_after") return `leaves the NAS on day ${daysOf(r)}`;
  if (o === "remove") return "leaves the NAS (copy in Azure)";
  return r.trigger === "deleted_at_source" ? "copy kept in Azure" : "stays on the NAS";
}

function RuleRow({
  rule,
  n,
  first,
  last,
  checks,
  onChange,
  onMove,
  onRemove,
}: {
  rule: Rule;
  n: number;
  first: boolean;
  last: boolean;
  checks: RuleCheck[];
  onChange: (r: Rule) => void;
  onMove: (by: -1 | 1) => void;
  onRemove: () => void;
}) {
  const id = useId();
  const c = rule.conditions;
  const setC = (patch: Partial<Conditions>) => {
    const next = Object.fromEntries(
      Object.entries({ ...c, ...patch }).filter(([, v]) => (v as unknown) !== undefined),
    ) as Conditions;
    onChange({ ...rule, conditions: next });
  };
  const num = (v: string) => (v === "" ? undefined : Number(v));
  const o = outcomeOf(rule);
  const onKey = (e: KeyboardEvent) => {
    if (!e.altKey) return;
    if (e.key === "ArrowUp" && !first) {
      e.preventDefault();
      onMove(-1);
    }
    if (e.key === "ArrowDown" && !last) {
      e.preventDefault();
      onMove(1);
    }
  };
  return (
    <fieldset className="card card-pad rule-row">
      <legend>
        Rule {n}
        {removes(rule) ? <span className="badge badge-warn"> leaves NAS</span> : null}
      </legend>
      <div className="grid-2">
        <div className="field">
          <label className="field-label" htmlFor={`${id}-trigger`}>
            When
          </label>
          <select
            onKeyDown={onKey}
            id={`${id}-trigger`}
            value={rule.trigger}
            onChange={(e) => {
              const t = e.target.value as Trigger;
              const first = OUTCOMES[t][0]?.value ?? "copy";
              onChange({ ...rule, trigger: t, actions: actionsFor(t, first, 30, []) });
            }}
          >
            {(Object.keys(TRIGGER_LABEL) as Trigger[]).map((t) => (
              <option key={t} value={t}>
                {TRIGGER_LABEL[t]}
              </option>
            ))}
          </select>
        </div>
        <div className="field">
          <label className="field-label" htmlFor={`${id}-then`}>
            Then
          </label>
          <select
            onKeyDown={onKey}
            id={`${id}-then`}
            value={o}
            onChange={(e) =>
              onChange({
                ...rule,
                actions: actionsFor(
                  rule.trigger,
                  e.target.value as Outcome,
                  daysOf(rule),
                  rule.actions,
                ),
              })
            }
          >
            {OUTCOMES[rule.trigger].map((x) => (
              <option key={x.value} value={x.value}>
                {x.label}
              </option>
            ))}
          </select>
        </div>
        {o === "copy_remove_after" ? (
          <div className="field">
            <label className="field-label" htmlFor={`${id}-days`}>
              Remove after (days)
            </label>
            <input
              onKeyDown={onKey}
              id={`${id}-days`}
              type="number"
              min={1}
              value={daysOf(rule)}
              onChange={(e) =>
                onChange({
                  ...rule,
                  actions: actionsFor(rule.trigger, o, Number(e.target.value), rule.actions),
                })
              }
            />
          </div>
        ) : null}
      </div>
      {rule.trigger !== "deleted_at_source" ? (
        <details open={chips(c).length > 0}>
          <summary>If ({chips(c).length ? chips(c).join(" · ") : "every file"})</summary>
          <div className="grid-2">
            <div className="field">
              <label className="field-label" htmlFor={`${id}-path`}>
                Path (globs, comma-separated)
              </label>
              <input
                onKeyDown={onKey}
                id={`${id}-path`}
                value={(c.path ?? []).join(", ")}
                placeholder="surveys/**/raw/*"
                onChange={(e) => setC({ path: list(e.target.value) })}
              />
            </div>
            <div className="field">
              <label className="field-label" htmlFor={`${id}-ext`}>
                Extensions
              </label>
              <input
                onKeyDown={onKey}
                id={`${id}-ext`}
                value={(c.extension ?? []).join(", ")}
                placeholder="segy, sgy"
                onChange={(e) => setC({ extension: list(e.target.value) })}
              />
            </div>
            <div className="field">
              <label className="field-label" htmlFor={`${id}-size`}>
                At least (GiB)
              </label>
              <input
                onKeyDown={onKey}
                id={`${id}-size`}
                type="number"
                min={0}
                value={c.size_gte === undefined ? "" : c.size_gte / GIB}
                onChange={(e) => {
                  const v = num(e.target.value);
                  setC({ size_gte: v === undefined ? undefined : Math.round(v * GIB) });
                }}
              />
            </div>
            <div className="field">
              <label className="field-label" htmlFor={`${id}-age`}>
                Arrived more than (days) ago
              </label>
              <input
                onKeyDown={onKey}
                id={`${id}-age`}
                type="number"
                min={0}
                value={c.age_since_arrival_days_gt ?? ""}
                onChange={(e) => setC({ age_since_arrival_days_gt: num(e.target.value) })}
              />
            </div>
            <div className="field">
              <label className="field-label" htmlFor={`${id}-mod`}>
                Not changed for (days)
              </label>
              <input
                onKeyDown={onKey}
                id={`${id}-mod`}
                type="number"
                min={0}
                value={c.modified_age_days_gt ?? ""}
                onChange={(e) => setC({ modified_age_days_gt: num(e.target.value) })}
              />
            </div>
            <div className="field">
              <label className="field-label" htmlFor={`${id}-acc`}>
                Not opened for (days)
              </label>
              <input
                onKeyDown={onKey}
                id={`${id}-acc`}
                type="number"
                min={0}
                value={c.accessed_age_days_gt ?? ""}
                onChange={(e) => setC({ accessed_age_days_gt: num(e.target.value) })}
              />
              <p className="field-hint">
                Many NAS mount with noatime or relatime, and the annex's own reads count as opens.
              </p>
            </div>
          </div>
        </details>
      ) : null}
      {checks.length ? (
        <ul className="callout callout-warn" aria-label={`Problems with rule ${n}`}>
          {checks.map((k) => (
            <li key={k.check}>
              <strong>{CHECK_LABEL[k.check] ?? k.check}</strong>: {k.message}
            </li>
          ))}
        </ul>
      ) : null}
      <div className="row-actions">
        <button
          type="button"
          className="btn btn-sm"
          disabled={first}
          onClick={() => onMove(-1)}
          aria-label={`Move rule ${n} up`}
        >
          Move up
        </button>
        <button
          type="button"
          className="btn btn-sm"
          disabled={last}
          onClick={() => onMove(1)}
          aria-label={`Move rule ${n} down`}
        >
          Move down
        </button>
        <button
          type="button"
          className="btn btn-sm btn-danger"
          onClick={onRemove}
          aria-label={`Remove rule ${n}`}
        >
          Remove
        </button>
      </div>
    </fieldset>
  );
}

/** The Out diagram (annex.md 8.3), drawn from the list, with the dry run's counts. */
export function Flow({ rules, report }: { rules: Rule[]; report: Report | null }) {
  const counts = new Map((report?.rules ?? []).map((r) => [r.id, r]));
  const groups = (Object.keys(TRIGGER_LABEL) as Trigger[])
    .map((t) => ({ t, rs: rules.filter((r) => r.trigger === t) }))
    .filter((g) => g.rs.length);
  return (
    <div className="rule-flow" aria-label="Rule flow">
      {groups.map(({ t, rs }) => (
        <section key={t}>
          <h3>{TRIGGER_LABEL[t]}</h3>
          <ol>
            {rs.map((r) => {
              const k = counts.get(r.id);
              const zero = report !== null && k !== undefined && k.files === 0;
              const sniffs = Boolean(r.conditions.content_type?.length);
              return (
                <li key={r.id} className={zero ? "muted" : undefined}>
                  <span className="chips">
                    {chips(r.conditions).length
                      ? chips(r.conditions).join(" · ")
                      : "everything else"}
                  </span>
                  {" → "}
                  <strong>
                    {OUTCOMES[r.trigger].find((x) => x.value === outcomeOf(r))?.label}
                  </strong>
                  {" → "}
                  <span className={removes(r) ? "badge badge-warn" : "badge badge-outline"}>
                    {endsUp(r)}
                  </span>
                  {k ? (
                    <span className="muted">
                      {" "}
                      · {zero ? "no files match" : countText(k, formatBytes)}
                    </span>
                  ) : null}
                  {sniffs ? (
                    <span className="muted">
                      {" "}
                      · tells the type by reading each new file&apos;s first 4 KB, which updates its
                      last-opened time on the NAS; previews count only types already known from
                      copies
                    </span>
                  ) : null}
                </li>
              );
            })}
          </ol>
        </section>
      ))}
    </div>
  );
}

function summary(r: Report | null): string {
  if (!r) return "Counting…";
  const left = r.removals;
  const undecided = r.undecided?.files ?? 0;
  return (
    `${countText(r.copied, formatBytes)} would be copied, ` +
    `${countText(r.excluded, formatBytes)} stay on the NAS only` +
    (left && left.files !== 0
      ? `, and ${countText(left, formatBytes)} would leave the NAS within a year`
      : "") +
    (undecided ? `; ${countText(r.undecided, formatBytes)} can't be decided yet.` : ".")
  );
}

/** The rule builder: an ordered, accessible list, a diagram drawn from it, live counts. */
export function RulesPage() {
  const { annexId = "" } = useParams();
  const navigate = useNavigate();
  const annex = useAnnex(annexId);
  const a = annex.data?.annex;
  usePageTitle(a ? `${a.name}: rules` : "Rules");
  const sets = useRulesets(annexId);
  const running = (sets.data?.items ?? []).find((r) => r.state === "active");
  const current = useRuleset(annexId, running ? running.version : null);
  const [rules, setRules] = useState<Rule[] | null>(null);
  const [rationale, setRationale] = useState("");
  const [runId, setRunId] = useState<string | null>(null);
  const [checks, setChecks] = useState<RuleCheck[]>([]);
  const dry = useCreateDryRun(annexId);
  const run = useDryRun(annexId, runId);
  const back = useReturnPreview(annexId);
  const [backReport, setBackReport] = useState<ReturnPreview | null>(null);
  const propose = useProposeRuleset(annexId);
  const rationaleId = useId();
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null);

  useEffect(() => {
    if (rules === null && current.data) setRules(rulesOf(current.data.body));
    if (rules === null && sets.data && !running) setRules(templateRules("copy"));
  }, [current.data, sets.data, running, rules]);

  const body = useMemo<RuleBody | null>(
    () => (rules ? ({ rules } as unknown as RuleBody) : null),
    [rules],
  );
  const { mutate: preview } = dry;
  const { mutate: previewBack } = back;
  // Live counts: a dry run a second after the last edit.
  useEffect(() => {
    if (!body) return;
    if (timer.current) clearTimeout(timer.current);
    timer.current = setTimeout(() => {
      preview(body, {
        onSuccess: (r) => {
          setRunId(r.run_id);
          setChecks(r.checks ?? []);
        },
      });
      // The Back half: the service's preview over the removed files.
      previewBack(body, { onSuccess: (r) => setBackReport(r) });
    }, 1000);
    return () => {
      if (timer.current) clearTimeout(timer.current);
    };
  }, [body, preview, previewBack]);

  if (annex.isError) return <ErrorCallout error={annex.error} />;
  if (!a || !rules) return <Loading />;
  const report = reportOf(run.data);
  // Counts over the NAS listing are an ArchiveAdmin's until the annex's
  // folders carry the NAS's own ACLs (annex.md section 4.5).
  const previewRefused = dry.error instanceof ApiError && dry.error.code === "role_required";
  const anyRemoval = rules.some(removes);
  const backRefused = back.error instanceof ApiError && back.error.code === "role_required";
  const set = (i: number, r: Rule) => setRules(rules.map((x, j) => (j === i ? r : x)));
  // Out and Back rules share one list; each moves among its own kind.
  const outIdx = rules.flatMap((r, i) => (isReturn(r) ? [] : [i]));
  const backIdx = rules.flatMap((r, i) => (isReturn(r) ? [i] : []));
  const move = (i: number, by: -1 | 1) => {
    const among = isReturn(rules[i] ?? { trigger: "" }) ? backIdx : outIdx;
    const j = among[among.indexOf(i) + by];
    if (j === undefined) return;
    const next = [...rules];
    const a = next[i];
    const b = next[j];
    if (!a || !b) return;
    next[i] = b;
    next[j] = a;
    setRules(next);
  };
  const backRules = backIdx.map((i) => rules[i] as unknown as ReturnRule);
  const setBack = (rs: ReturnRule[]) =>
    setRules([...rules.filter((r) => !isReturn(r)), ...(rs as unknown as Rule[])]);
  const failed =
    propose.error instanceof ApiError
      ? ((propose.error.problem as { rule_checks?: RuleCheck[] } | undefined)?.rule_checks ?? [])
      : [];

  async function save() {
    if (!body) return;
    try {
      const r = await propose.mutateAsync({ body, ...(rationale ? { rationale } : {}) });
      void navigate(`/annexes/${annexId}?proposal=${r.version}`);
    } catch {
      // Shown below the button, and next to the rules a check names.
    }
  }

  return (
    <div className="page">
      <div className="page-head">
        <div>
          <p className="crumbs">
            <Link to="/annexes">Annexes</Link> / <Link to={`/annexes/${annexId}`}>{a.name}</Link>
          </p>
          <h1>Rules</h1>
          <p className="page-question">What happens to each file in this folder, and when?</p>
          <p className="lede mono">{a.unc}</p>
        </div>
      </div>
      <p>
        The first rule that matches a file decides what happens to it. Saving makes a proposal:
        nothing changes until it is approved.
      </p>
      <div className="row-actions" role="group" aria-label="Start from a template">
        <button
          type="button"
          className="btn btn-sm"
          onClick={() => setRules(templateRules("copy"))}
        >
          Start from Copy
        </button>
        <button
          type="button"
          className="btn btn-sm"
          onClick={() => setRules(templateRules("archive_after", 30))}
        >
          Start from Archive after 30 days
        </button>
        <button
          type="button"
          className="btn btn-sm"
          onClick={() => setRules(templateRules("move_now"))}
        >
          Start from Move now
        </button>
      </div>

      <section className="card card-pad" aria-labelledby="flow-h">
        <h2 id="flow-h">Out: from the NAS to Azure</h2>
        <p aria-live="polite" className="lede">
          {previewRefused
            ? "Live counts are an ArchiveAdmin's: they can show what is in NAS folders you can't read. The rules are checked when you save them."
            : summary(report)}
        </p>
        <Flow rules={rules} report={report} />
      </section>

      {anyRemoval ? (
        <div className="callout callout-warn" role="note">
          Rules that remove files from the NAS need an ArchiveAdmin&apos;s approval, then run as a
          dry run first, and start only when an ArchiveAdmin confirms the dry run. In this release
          nothing is removed: the annex records what it would remove.
        </div>
      ) : null}

      <section aria-labelledby="rules-h">
        <h2 id="rules-h">Rules, in order</h2>
        <p className="field-hint">Alt+↑ and Alt+↓ move the rule you are in.</p>
        {outIdx.map((i, n) => {
          const r = rules[i] as Rule;
          return (
            <RuleRow
              key={r.id}
              rule={r}
              n={n + 1}
              first={n === 0}
              last={n === outIdx.length - 1}
              checks={[...checks, ...failed].filter((k) => k.rule === r.id)}
              onChange={(x) => set(i, x)}
              onMove={(by) => move(i, by)}
              onRemove={() => setRules(rules.filter((_, j) => j !== i))}
            />
          );
        })}
        <button
          type="button"
          className="btn"
          onClick={() =>
            setRules([
              { id: newId("arrived"), trigger: "arrived", conditions: {}, actions: [TIER] },
              ...rules,
            ])
          }
        >
          + Rule
        </button>
      </section>

      <section className="card card-pad" aria-labelledby="back-h">
        <h2 id="back-h">Back: when a removed file is recalled</h2>
        <p className="lede" aria-live="polite">
          {backRefused
            ? "Counts over the removed files are an ArchiveAdmin's. The rules are checked when you save them."
            : backReport
              ? `${backReport.files} removed files so far${backReport.truncated ? " (counted up to 200,000)" : ""}; ${backReport.placed_nowhere.files ?? 0} would be placed nowhere.`
              : "Counting…"}
        </p>
        <BackFlow rules={backRules} preview={backRefused ? null : backReport} />
        <p className="field-hint">
          A file is placed only where the person who asks for it could add a file themselves. They
          may pick among the places a rule lists.
        </p>
        <div className="row-actions" role="group" aria-label="Start the Back rules from a template">
          <button
            type="button"
            className="btn btn-sm"
            onClick={() => setBack(returnTemplate("origin"))}
          >
            Back where it came from
          </button>
          <button
            type="button"
            className="btn btn-sm"
            onClick={() => setBack(returnTemplate("restore_folder"))}
          >
            Into a restore folder
          </button>
          <button
            type="button"
            className="btn btn-sm"
            onClick={() => setBack(returnTemplate("share"))}
          >
            Into an OmniFile share
          </button>
        </div>
        {backIdx.map((i, n) => {
          const r = rules[i] as unknown as ReturnRule;
          return (
            <ReturnRuleRow
              key={r.id}
              rule={r}
              n={n + 1}
              first={n === 0}
              last={n === backIdx.length - 1}
              checks={[...checks, ...failed].filter((k) => k.rule === r.id)}
              onChange={(x) => set(i, x as unknown as Rule)}
              onMove={(by) => move(i, by)}
              onRemove={() => setRules(rules.filter((_, j) => j !== i))}
            />
          );
        })}
        <button
          type="button"
          className="btn"
          onClick={() => setRules([...rules, newReturnRule() as unknown as Rule])}
        >
          + Back rule
        </button>
      </section>

      <section className="card card-pad" aria-labelledby="save-h">
        <h2 id="save-h">Propose</h2>
        <div className="field">
          <label className="field-label" htmlFor={rationaleId}>
            Why (for the approver)
          </label>
          <textarea
            id={rationaleId}
            value={rationale}
            maxLength={2000}
            onChange={(e) => setRationale(e.target.value)}
          />
        </div>
        {propose.isError ? <ErrorCallout error={propose.error} /> : null}
        <button
          type="button"
          className="btn btn-primary"
          disabled={propose.isPending}
          onClick={() => void save()}
        >
          Save as a proposal
        </button>
      </section>
    </div>
  );
}
