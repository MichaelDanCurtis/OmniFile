import { useQueryClient } from "@tanstack/react-query";
import { useId, useMemo, useState, type ReactNode } from "react";
import { Link, useParams } from "react-router";
import { ApiError, api, unwrap } from "../../api/client";
import {
  qk,
  useActivePolicy,
  useCreatePolicy,
  useDatasets,
  usePolicyHistory,
  usePolicyPreview,
  useShares,
  useSubmitProposal,
  type PreviewInput,
} from "../../api/queries";
import type {
  CostAssumptions,
  Dataset,
  OnOpen,
  PolicyPreview,
  PolicyVersion,
  Share,
  ShareOverride,
} from "../../api/types";
import { useSession } from "../../app/session";
import { usePageTitle } from "../../app/usePageTitle";
import { Callout, Empty, ErrorCallout, Loading, problemText } from "../../components/Feedback";
import { GroupPicker, groupName } from "../../components/GroupPicker";
import { Modal } from "../../components/Modal";
import { TierChip } from "../../components/TierChip";
import { useToast } from "../../components/Toasts";
import { severityCounts } from "../../components/Checks";
import {
  backendLabel,
  formatDateTime,
  formatMoney,
  layoutLabel,
  migrationLabel,
  pluralize,
} from "../../lib/format";
import { canSeePrices, isArchiveAdmin } from "../../lib/permissions";
import { useDebouncedJson } from "../../lib/useDebounced";
import { PreviewPane } from "./PreviewPane";
import { LazyPart, lazyNamed } from "../../app/lazy";
import {
  PRESETS,
  bodyToForm,
  diffBodies,
  fieldElementId,
  fieldLabel,
  formToBody,
  isM1Policy,
  showValue,
  type PolicyForm,
} from "./policyForm";

// The timeline is its own chunk (app/lazy.tsx); the day inputs below never wait for it.
const PolicyTimeline = lazyNamed(() => import("./visual/PolicyTimeline"), "PolicyTimeline");

/** The debounce before asking the server for a fresh preview (M4.md: 250 ms). */
export const PREVIEW_DEBOUNCE_MS = 250;

export function PolicyEditorPage() {
  const { datasetId = "" } = useParams();
  const datasets = useDatasets();
  const active = useActivePolicy(datasetId);
  const shares = useShares();
  const dataset = datasets.data?.items.find((d) => d.dataset_id === datasetId);
  usePageTitle(dataset ? `Policy · ${dataset.name}` : "Policy");

  const noPolicy =
    active.isError && active.error instanceof ApiError && active.error.status === 404;
  if (datasets.isPending || shares.isPending || (active.isPending && !noPolicy)) return <Loading />;
  if (datasets.isError) return <PageError error={datasets.error} />;
  if (shares.isError) return <PageError error={shares.error} />;
  if (active.isError && !noPolicy) return <PageError error={active.error} />;
  if (!dataset) {
    return (
      <div className="page">
        <Empty title="No such dataset" />
      </div>
    );
  }
  const datasetShares = shares.data.items.filter((s) => s.dataset_id === dataset.dataset_id);
  const policy = noPolicy ? null : (active.data ?? null);
  return (
    <PolicyEditor
      // Keyed by dataset only: a save mustn't remount the editor before it has
      // reported every part (the policy, then each share).
      key={dataset.dataset_id}
      dataset={dataset}
      active={policy}
      shares={datasetShares}
    />
  );
}

function PageError({ error }: { error: unknown }) {
  return (
    <div className="page">
      <ErrorCallout error={error} />
    </div>
  );
}

interface OpenSetting {
  on_open: OnOpen;
  threshold: string;
}

function shareOpenSetting(share: Share): OpenSetting {
  return { on_open: share.on_open, threshold: String(share.wait_threshold_minutes ?? 15) };
}

function goTo(field: string, shareId?: string | null) {
  const el = document.getElementById(fieldElementId(field, shareId));
  if (!el) return;
  const details = el.closest("details");
  if (details && !details.open) details.open = true;
  el.scrollIntoView({ block: "center", behavior: "smooth" });
  el.focus({ preventScroll: true });
}

function layoutText(dataset: Dataset): string {
  const migration =
    dataset.migration_state && dataset.migration_state !== "steady"
      ? ` · ${migrationLabel(dataset.migration_state)}`
      : "";
  return `${backendLabel(dataset.backend_kind)} · ${layoutLabel(dataset.storage_layout)}${
    dataset.private_network ? " · private endpoints" : ""
  }${migration}`;
}

function PolicyEditor({
  dataset,
  active,
  shares,
}: {
  dataset: Dataset;
  active: PolicyVersion | null;
  shares: Share[];
}) {
  const { me } = useSession();
  const admin = isArchiveAdmin(me);
  const toast = useToast();
  const client = useQueryClient();
  const createPolicy = useCreatePolicy(dataset.dataset_id);
  const m1 = isM1Policy(active?.body);
  const [converted, setConverted] = useState(false);
  const [form, setForm] = useState<PolicyForm>(() => bodyToForm(active?.body));
  const initialOpen = useMemo(
    () => Object.fromEntries(shares.map((s) => [s.share_id, shareOpenSetting(s)])),
    [shares],
  );
  const [open, setOpen] = useState<Record<string, OpenSetting>>(initialOpen);
  const [assume, setAssume] = useState({ tb: "1", meanGb: "1", recallPct: "0" });
  const [note, setNote] = useState("");
  const [proposing, setProposing] = useState(false);
  const [saveError, setSaveError] = useState<unknown>(null);
  const [saveReport, setSaveReport] = useState<SaveReport | null>(null);
  const worm = shares.some((s) => s.worm.enabled);

  const set = <K extends keyof PolicyForm>(key: K, value: PolicyForm[K]) =>
    setForm((f) => ({ ...f, [key]: value }));

  const body = formToBody(form);
  const changes = m1 && !converted ? [] : diffBodies(active?.body, body);
  const overrides: ShareOverride[] = shares
    .filter((s) => {
      const o = open[s.share_id];
      const i = initialOpen[s.share_id];
      return (
        o && i && (o.on_open !== i.on_open || (o.on_open === "auto" && o.threshold !== i.threshold))
      );
    })
    .map((s) => {
      const o = open[s.share_id] ?? shareOpenSetting(s);
      return {
        share_id: s.share_id,
        on_open: o.on_open,
        wait_threshold_minutes: o.on_open === "auto" ? Number(o.threshold) || null : null,
      };
    });
  const assumptions: CostAssumptions = {
    tb: Number(assume.tb) > 0 ? Number(assume.tb) : 1,
    mean_file_bytes: Math.max(1, Math.round((Number(assume.meanGb) || 1) * 1e9)),
    recall_fraction_per_year: Math.min(1, Math.max(0, (Number(assume.recallPct) || 0) / 100)),
  };
  const input: PreviewInput = {
    dataset_id: dataset.dataset_id,
    body,
    ...(overrides.length ? { share_overrides: overrides } : {}),
    assumptions,
  };
  const debounced = useDebouncedJson(input, PREVIEW_DEBOUNCE_MS);
  const preview = usePolicyPreview(m1 && !converted ? null : debounced);
  const settled = JSON.stringify(input) === JSON.stringify(debounced) && !preview.isPlaceholderData;
  const hasErrors = !!preview.data && !preview.data.valid;
  const dirty = changes.length > 0 || overrides.length > 0;
  const shareName = (id: string) => shares.find((s) => s.share_id === id)?.name;

  /**
   * Saves the policy (if it changed), then each share's "what users see". They
   * are separate requests, so the report says exactly which parts were saved.
   */
  async function save() {
    setSaveError(null);
    setSaveReport(null);
    let version: number | null = null;
    if (changes.length > 0) {
      try {
        const policy = await createPolicy.mutateAsync({
          body,
          base_version: active?.version ?? null,
          ...(note.trim() ? { note: note.trim() } : {}),
        });
        version = policy.version;
        setNote("");
      } catch (error) {
        setSaveError(error); // nothing was saved: the shares wait for the policy
        return;
      }
    }
    const saved: string[] = [];
    const failed: { name: string; error: unknown }[] = [];
    for (const o of overrides) {
      const share = shares.find((s) => s.share_id === o.share_id);
      if (!share) continue;
      try {
        const updated = await unwrap(
          api.PATCH("/v1/shares/{share_id}/settings", {
            params: { path: { share_id: share.share_id }, header: { "If-Match": share.etag } },
            body: { on_open: o.on_open, wait_threshold_minutes: o.wait_threshold_minutes ?? null },
          }),
        );
        client.setQueryData(qk.share(share.share_id), updated);
        saved.push(share.name);
      } catch (error) {
        failed.push({ name: share.name, error });
      }
    }
    if (overrides.length > 0) await client.invalidateQueries({ queryKey: qk.shares });
    if (failed.length === 0) {
      toast(
        [
          version !== null
            ? `Version ${version} is active. The scheduler is re-planning ${dataset.name}.`
            : null,
          saved.length ? `Saved what people see on ${saved.join(", ")}.` : null,
        ]
          .filter(Boolean)
          .join(" "),
      );
    } else {
      setSaveReport({ version, saved, failed });
    }
  }

  if (m1 && !converted) {
    return (
      <div className="page">
        <EditorHead dataset={dataset} active={active} />
        <section className="card card-pad stack">
          <h2>This dataset still has an M1 policy</h2>
          <p>
            It frees the edge copy {active?.body.stub_after_minutes ?? "?"} minutes after upload and
            has no tiers or archive day. Convert it to a scheduled policy to use tiers and Archive.
          </p>
          <pre className="json">{JSON.stringify(active?.body, null, 2)}</pre>
          {admin ? (
            <div className="row">
              <button type="button" className="btn btn-primary" onClick={() => setConverted(true)}>
                Convert to a scheduled policy
              </button>
            </div>
          ) : null}
        </section>
      </div>
    );
  }

  return (
    <div className="page">
      <EditorHead dataset={dataset} active={active} />
      <div className="editor">
        <div className="editor-form">
          <Question n={1} title="Which files does this cover?">
            <p>
              Everything in <strong>{dataset.name}</strong>, on{" "}
              {shares.length === 0
                ? "no shares yet"
                : shares.map((s, i) => (
                    <span key={s.share_id}>
                      {i > 0 ? (i === shares.length - 1 ? " and " : ", ") : ""}
                      <span className="mono">{s.name}</span>
                    </span>
                  ))}
              .
            </p>
            <p className="field-hint">
              {layoutText(dataset)}. Choosing files by folder, type, group or size comes in a later
              release; today one policy covers the whole dataset.
            </p>
          </Question>

          <Question n={2} title="When should files move to cheaper storage?">
            <fieldset className="options options-row">
              <legend className="sr-only">How files move</legend>
              <label className="option">
                <input
                  id="f-mode"
                  type="radio"
                  name="mode"
                  checked={form.mode === "scheduled"}
                  onChange={() => set("mode", "scheduled")}
                />
                <span className="option-title">On a schedule I set</span>
                <span className="option-body">You choose the day for Cool, Cold and Archive.</span>
              </label>
              <label className="option">
                <input
                  type="radio"
                  name="mode"
                  checked={form.mode === "smart"}
                  disabled={worm}
                  onChange={() => set("mode", "smart")}
                />
                <span className="option-title">Smart, then Archive on a fixed day</span>
                <span className="option-body">
                  {worm
                    ? "Not available: this dataset has a WORM share."
                    : "Azure moves files among Hot, Cool and Cold by use. The archive day is always kept."}
                </span>
              </label>
            </fieldset>

            <LazyPart label="Loading the timeline…">
              <PolicyTimeline
                form={form}
                onDays={(days) => setForm((f) => ({ ...f, ...days }))}
                preview={preview.data}
              />
            </LazyPart>

            {form.mode === "scheduled" ? (
              <>
                <div className="sentence">
                  <label htmlFor="f-clock">Count days from</label>
                  <select
                    id="f-clock"
                    value={form.clock}
                    onChange={(e) => set("clock", e.target.value as PolicyForm["clock"])}
                  >
                    <option value="last_read">a file's last read</option>
                    <option value="written">when a file was written</option>
                  </select>
                </div>
                <div className="stack" style={{ gap: 6 }}>
                  <DayRow
                    id="f-warm_day"
                    tier="warm"
                    enabled={form.useWarm}
                    onToggle={(v) => set("useWarm", v)}
                    value={form.warmDay}
                    onChange={(v) => set("warmDay", v)}
                    label="Move to Cool after"
                  />
                  <DayRow
                    id="f-cold_day"
                    tier="cold"
                    enabled={form.useCold}
                    onToggle={(v) => set("useCold", v)}
                    value={form.coldDay}
                    onChange={(v) => set("coldDay", v)}
                    label="Move to Cold after"
                  />
                  <DayRow
                    id="f-archive_day"
                    tier="archive"
                    value={form.archiveDay}
                    onChange={(v) => set("archiveDay", v)}
                    label="Archive after"
                  />
                </div>
                <div className="presets" role="group" aria-label="Start from a preset">
                  <span className="field-hint">Start from:</span>
                  {PRESETS.map((p) => (
                    <button
                      key={p.id}
                      type="button"
                      className="btn btn-sm"
                      title={p.hint}
                      onClick={() =>
                        setForm((f) => ({
                          ...f,
                          useWarm: true,
                          useCold: true,
                          warmDay: String(p.warm),
                          coldDay: String(p.cold),
                          archiveDay: String(p.archive),
                        }))
                      }
                    >
                      {p.label} <span className="muted">· {p.hint}</span>
                    </button>
                  ))}
                </div>
              </>
            ) : (
              <div className="stack" style={{ gap: 6 }}>
                <DayRow
                  id="f-archive_day"
                  tier="archive"
                  value={form.archiveDay}
                  onChange={(v) => set("archiveDay", v)}
                  label="Archive"
                  suffix="days after a file is written"
                />
                <p className="field-hint">
                  With Smart the archive day counts from the write, and a file that's given a tier
                  by hand leaves Smart for good.
                </p>
              </div>
            )}
          </Question>

          <Question n={3} title="What do people see when they open an archived file?">
            {shares.length === 0 ? (
              <p className="muted">This dataset has no shares yet.</p>
            ) : (
              shares.map((share) => (
                <OpenBehaviour
                  key={share.share_id}
                  share={share}
                  value={open[share.share_id] ?? shareOpenSetting(share)}
                  editable
                  proposing={!admin}
                  onChange={(v) => setOpen((all) => ({ ...all, [share.share_id]: v }))}
                />
              ))
            )}
            <p className="field-hint">
              Whatever you choose, a <span className="mono">.RECALLING.txt</span> note appears next
              to the file, the person who opened it gets a Teams message, and the portal shows the
              recall's four steps. Archived files never look deleted.
            </p>
          </Question>

          <Question n={4} title="Who is told when a recall starts?">
            <div className="stack" style={{ gap: 4 }}>
              <label className="check-row">
                <input
                  id="f-notify-requester"
                  type="checkbox"
                  checked={form.notifyRequester}
                  onChange={(e) => set("notifyRequester", e.target.checked)}
                />
                <span>
                  The person who opened the file
                  <span className="field-hint">
                    A Teams card with Recall, Keep online and Track buttons.
                  </span>
                </span>
              </label>
              <label className="check-row">
                <input
                  id="f-notify-owners"
                  type="checkbox"
                  checked={form.notifyOwners}
                  onChange={(e) => set("notifyOwners", e.target.checked)}
                />
                <span>The share's Owners</span>
              </label>
              <div className="check-row" style={{ cursor: "default" }}>
                <span style={{ width: 16 }} />
                <div className="stack" style={{ gap: 6, flex: 1 }}>
                  <span id="f-notify-watchers_group_sid" tabIndex={-1}>
                    A watchers group:{" "}
                    <strong>
                      {form.watchersSid
                        ? (form.watchersName ?? <span className="mono">{form.watchersSid}</span>)
                        : "none"}
                    </strong>
                    {form.watchersSid ? (
                      <>
                        {" "}
                        <button
                          type="button"
                          className="btn-link"
                          onClick={() =>
                            setForm((f) => ({ ...f, watchersSid: null, watchersName: null }))
                          }
                        >
                          Remove
                        </button>
                      </>
                    ) : null}
                  </span>
                  {admin || me.owner_of.length > 0 ? (
                    <GroupPicker
                      label="Find a watchers group"
                      onPick={(g) =>
                        setForm((f) => ({ ...f, watchersSid: g.sid, watchersName: groupName(g) }))
                      }
                    />
                  ) : null}
                </div>
              </div>
              <label className="check-row">
                <input
                  id="f-notify-email_fallback"
                  type="checkbox"
                  checked={form.emailFallback}
                  onChange={(e) => set("emailFallback", e.target.checked)}
                />
                <span>
                  Email them when Teams can't reach them
                  <span className="field-hint">
                    For example, when the Teams app isn't installed for that person.
                  </span>
                </span>
              </label>
            </div>
          </Question>

          <details className="more">
            <summary>More settings</summary>
            <div className="more-body">
              <fieldset className="options">
                <legend className="field-label">
                  When should the gateway free its local copy?
                </legend>
                {(
                  [
                    [
                      "archive",
                      "When the file reaches Archive",
                      "Recommended. Reads before then never wait.",
                    ],
                    [
                      "leaves_hot",
                      "As soon as it leaves Hot",
                      "Frees disk sooner; reads of Cool and Cold files fetch from the cloud.",
                    ],
                    [
                      "never",
                      "Never",
                      "Every file stays whole on the gateway; only the cloud copy tiers.",
                    ],
                  ] as const
                ).map(([value, title, hint], i) => (
                  <label key={value} className="option">
                    <input
                      id={i === 0 ? "f-stub_at" : undefined}
                      type="radio"
                      name="stub_at"
                      checked={form.stubAt === value}
                      onChange={() => set("stubAt", value)}
                    />
                    <span className="option-title">{title}</span>
                    <span className="option-body">{hint}</span>
                  </label>
                ))}
              </fieldset>
              <div className="sentence">
                <label htmlFor="f-resident_mb">Keep the first</label>
                <input
                  id="f-resident_mb"
                  className="inline-num"
                  type="number"
                  min={0}
                  step={1}
                  value={form.residentMb}
                  onChange={(e) => set("residentMb", e.target.value)}
                />
                <span>
                  MB of each file on the gateway, so headers (SEG-Y) read without a recall.
                </span>
              </div>
              <div className="sentence">
                <label htmlFor="f-restub_after_hours">After a recall, free the copy again</label>
                <input
                  id="f-restub_after_hours"
                  className="inline-num"
                  type="number"
                  min={0}
                  step={1}
                  value={form.restubAfterHours}
                  onChange={(e) => set("restubAfterHours", e.target.value)}
                />
                <span>hours after the last read.</span>
              </div>
              <fieldset className="options">
                <legend className="field-label">Cost assumptions</legend>
                <div className="sentence">
                  <label htmlFor="f-tb">Data size</label>
                  <input
                    id="f-tb"
                    className="inline-num"
                    type="number"
                    min={0.001}
                    step="any"
                    value={assume.tb}
                    onChange={(e) => setAssume((a) => ({ ...a, tb: e.target.value }))}
                  />
                  <span>TB, average file</span>
                  <label htmlFor="f-mean" className="sr-only">
                    Average file size in GB
                  </label>
                  <input
                    id="f-mean"
                    className="inline-num"
                    type="number"
                    min={0.000001}
                    step="any"
                    value={assume.meanGb}
                    onChange={(e) => setAssume((a) => ({ ...a, meanGb: e.target.value }))}
                  />
                  <span>GB, recalled</span>
                  <label htmlFor="f-recall" className="sr-only">
                    Percent recalled per year
                  </label>
                  <input
                    id="f-recall"
                    className="inline-num"
                    type="number"
                    min={0}
                    max={100}
                    step="any"
                    value={assume.recallPct}
                    onChange={(e) => setAssume((a) => ({ ...a, recallPct: e.target.value }))}
                  />
                  <span>% a year.</span>
                </div>
              </fieldset>
              <div className="stack" style={{ gap: 6 }}>
                <span className="field-label">The policy as stored</span>
                <pre className="json" aria-label="Policy body as JSON">
                  {JSON.stringify(body, null, 2)}
                </pre>
              </div>
            </div>
          </details>

          <History datasetId={dataset.dataset_id} />

          <div className="save-bar" role="region" aria-label="Save">
            <div className="stack" style={{ gap: 2, flex: 1, minWidth: 220 }}>
              {preview.data ? <LiveSummary preview={preview.data} /> : null}
              <strong>
                {dirty
                  ? `${pluralize(changes.length + overrides.length, "change")} from ${active ? `version ${active.version}` : "nothing"}`
                  : "No changes"}
              </strong>
              {changes.length > 0 ? (
                <span className="muted" data-testid="change-summary">
                  {changes
                    .slice(0, 3)
                    .map((c) => `${fieldLabel(c.field)}: ${showValue(c.from)} → ${showValue(c.to)}`)
                    .join(" · ")}
                  {changes.length > 3 ? ` · and ${changes.length - 3} more` : ""}
                </span>
              ) : null}
            </div>
            {admin && changes.length > 0 ? (
              <>
                <label htmlFor="f-note" className="sr-only">
                  Note for the history
                </label>
                <input
                  id="f-note"
                  type="text"
                  placeholder="Note for the history (optional)"
                  value={note}
                  maxLength={4000}
                  onChange={(e) => setNote(e.target.value)}
                />
              </>
            ) : null}
            <button
              type="button"
              className="btn"
              disabled={!dirty}
              onClick={() => {
                setForm(bodyToForm(active?.body));
                setOpen(initialOpen);
              }}
            >
              Reset
            </button>
            {admin ? (
              <button
                type="button"
                className="btn btn-primary"
                disabled={!dirty || hasErrors || !settled || createPolicy.isPending}
                onClick={() => void save()}
              >
                {createPolicy.isPending
                  ? "Saving…"
                  : changes.length > 0
                    ? `Save as version ${(active?.version ?? 0) + 1}`
                    : "Save"}
              </button>
            ) : (
              <button
                type="button"
                className="btn btn-primary"
                disabled={!dirty || hasErrors || !settled}
                onClick={() => setProposing(true)}
              >
                Propose this change…
              </button>
            )}
          </div>
          {hasErrors && dirty ? (
            <p className="field-error" role="alert">
              Fix the checks marked “to fix” before saving.
            </p>
          ) : null}
          {saveError ? <SaveError error={saveError} /> : null}
          {saveReport ? <PartialSave report={saveReport} /> : null}
        </div>

        <div className="editor-preview" aria-label="Live preview">
          {preview.isError ? (
            <ErrorCallout error={preview.error} title="The preview isn't available" />
          ) : !preview.data ? (
            <div className="card card-pad">
              <Loading label="Drawing the timeline…" />
            </div>
          ) : (
            <PreviewPane
              preview={preview.data}
              updating={!settled || preview.isFetching}
              onGo={goTo}
              shareName={shareName}
            />
          )}
        </div>
      </div>
      {proposing ? (
        <ProposeDialog
          onClose={() => setProposing(false)}
          dataset={dataset}
          body={body}
          baseVersion={active?.version ?? null}
          changes={changes.length + overrides.length}
          overrides={overrides}
        />
      ) : null}
    </div>
  );
}

/** The preview's headline in the sticky save bar, so it stays in view on a tablet. */
function LiveSummary({ preview }: { preview: PolicyPreview }) {
  const { me } = useSession();
  const counts = severityCounts(preview.checks);
  return (
    <span className="row live-summary" aria-hidden="true">
      {canSeePrices(me) && preview.cost.per_tb_first_year ? (
        <span className="tabular">
          <strong>{formatMoney(preview.cost.per_tb_first_year)}</strong> per TB, first year
        </span>
      ) : null}
      {counts.error ? <span className="badge badge-danger">{counts.error} to fix</span> : null}
      {counts.warning ? <span className="badge badge-warn">{counts.warning} warnings</span> : null}
    </span>
  );
}

interface SaveReport {
  version: number | null;
  saved: string[];
  failed: { name: string; error: unknown }[];
}

function PartialSave({ report }: { report: SaveReport }) {
  return (
    <Callout tone="warn" title="Partly saved" role="alert">
      <ul>
        {report.version !== null ? (
          <li>Saved: version {report.version} of the policy is active.</li>
        ) : null}
        {report.saved.length ? <li>Saved what people see on {report.saved.join(", ")}.</li> : null}
        {report.failed.map((f) => (
          <li key={f.name}>
            Not saved for {f.name}: {problemText(f.error)}
          </li>
        ))}
      </ul>
      <p>Save again to retry what failed; the saved parts won't be repeated.</p>
    </Callout>
  );
}

function SaveError({ error }: { error: unknown }) {
  if (error instanceof ApiError && error.code === "version_conflict") {
    return (
      <Callout
        tone="warn"
        title="Someone saved a newer version while you were editing"
        role="alert"
      >
        <p>{error.problem?.detail}</p>
        <p>Reload to start from the new version. Your edits here will be lost.</p>
        <button
          type="button"
          className="btn btn-sm"
          style={{ marginTop: 8 }}
          onClick={() => window.location.reload()}
        >
          Reload
        </button>
      </Callout>
    );
  }
  return <ErrorCallout error={error} title="Not saved" />;
}

function EditorHead({ dataset, active }: { dataset: Dataset; active: PolicyVersion | null }) {
  return (
    <>
      <nav aria-label="Breadcrumb">
        <ol className="crumbs">
          <li>
            <Link to="/policies">Policies</Link>
          </li>
          <li>
            <span aria-current="page">{dataset.name}</span>
          </li>
        </ol>
      </nav>
      <div className="page-head">
        <div>
          <h1>Policy for {dataset.name}</h1>
          <p className="page-question">When should files here cool down, and when reach Archive?</p>
          <p className="lede">
            {active
              ? `Version ${active.version} active since ${formatDateTime(active.approved_at)}, approved by ${active.approved_by.display_name ?? "an admin"}.`
              : "No policy yet: files stay where they are until you save one."}
          </p>
        </div>
        {dataset.pending_proposals ? (
          <div className="actions">
            <Link className="btn" to={`/proposals?dataset=${dataset.dataset_id}`}>
              {pluralize(dataset.pending_proposals, "pending proposal")}
            </Link>
          </div>
        ) : null}
      </div>
    </>
  );
}

function Question({ n, title, children }: { n: number; title: string; children: ReactNode }) {
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

function DayRow({
  id,
  tier,
  label,
  value,
  onChange,
  enabled = true,
  onToggle,
  suffix = "days",
}: {
  id: string;
  tier: "warm" | "cold" | "archive";
  label: string;
  value: string;
  onChange: (v: string) => void;
  enabled?: boolean;
  onToggle?: (v: boolean) => void;
  suffix?: string;
}) {
  return (
    <div className="sentence">
      {onToggle ? (
        <input
          type="checkbox"
          checked={enabled}
          aria-label={`Use ${tier === "warm" ? "Cool" : "Cold"}`}
          onChange={(e) => onToggle(e.target.checked)}
        />
      ) : (
        <span style={{ width: 16 }} aria-hidden="true" />
      )}
      <TierChip tier={tier} showNative={false} />
      <label htmlFor={id} className={enabled ? undefined : "muted"}>
        {label}
      </label>
      <input
        id={id}
        className="inline-num"
        type="number"
        inputMode="numeric"
        min={0}
        step={1}
        value={value}
        disabled={!enabled}
        onChange={(e) => onChange(e.target.value)}
      />
      <span className={enabled ? undefined : "muted"}>{suffix}</span>
      {!enabled ? <span className="muted">(skipped)</span> : null}
    </div>
  );
}

function OpenBehaviour({
  share,
  value,
  onChange,
  editable,
  proposing,
}: {
  share: Share;
  value: OpenSetting;
  onChange: (v: OpenSetting) => void;
  editable: boolean;
  proposing: boolean;
}) {
  const name = `on_open-${share.share_id}`;
  return (
    <fieldset className="options">
      <legend className="field-label">
        On <span className="mono">{share.name}</span>
        {proposing ? (
          <span className="muted"> (part of your proposal; applied to the share on approval)</span>
        ) : !editable ? (
          <span className="muted"> (only its Owners or an ArchiveAdmin can change this)</span>
        ) : null}
      </legend>
      <div className="options options-row">
        <label className="option">
          <input
            id={`f-on_open-${share.share_id}`}
            type="radio"
            name={name}
            disabled={!editable}
            checked={value.on_open === "fail"}
            onChange={() => onChange({ ...value, on_open: "fail" })}
          />
          <span className="option-title">Tell them straight away</span>
          <span className="option-body">
            The open fails fast, with a note saying it's on its way back. Best for people.
          </span>
        </label>
        <label className="option">
          <input
            type="radio"
            name={name}
            disabled={!editable}
            checked={value.on_open === "wait"}
            onChange={() => onChange({ ...value, on_open: "wait" })}
          />
          <span className="option-title">Make the program wait</span>
          <span className="option-body">
            The read waits until the file is back, even for hours. Best for batch clusters.
          </span>
        </label>
        <label className="option">
          <input
            type="radio"
            name={name}
            disabled={!editable}
            checked={value.on_open === "auto"}
            onChange={() => onChange({ ...value, on_open: "auto" })}
          />
          <span className="option-title">Wait only if it's quick</span>
          <span className="option-body">
            Waits when the recall is expected within a limit, otherwise fails fast.
          </span>
        </label>
      </div>
      {value.on_open === "auto" ? (
        <div className="sentence">
          <label htmlFor={`f-threshold-${share.share_id}`}>Wait at most</label>
          <input
            id={`f-threshold-${share.share_id}`}
            className="inline-num"
            type="number"
            min={1}
            max={1440}
            disabled={!editable}
            value={value.threshold}
            onChange={(e) => onChange({ ...value, threshold: e.target.value })}
          />
          <span>minutes.</span>
        </div>
      ) : null}
    </fieldset>
  );
}

function History({ datasetId }: { datasetId: string }) {
  const history = usePolicyHistory(datasetId);
  const items = history.data?.items ?? [];
  if (items.length === 0) return null;
  return (
    <details className="more">
      <summary>History ({pluralize(items.length, "version")})</summary>
      <div className="more-body">
        <ol className="versions" aria-label="Policy versions">
          {items.map((p) => (
            <li key={p.policy_id} className="version">
              <div className="version-main">
                <span className="version-title">
                  Version {p.version}
                  {p.state === "active" ? <span className="badge badge-accent">Active</span> : null}
                  {p.proposal_id ? (
                    <Link className="badge badge-outline" to={`/proposals/${p.proposal_id}`}>
                      from proposal
                    </Link>
                  ) : null}
                </span>
                <span className="muted num">{formatDateTime(p.approved_at)}</span>
              </div>
              <span className="muted">
                {p.approved_by.display_name ?? "—"}
                {p.note ? ` · ${p.note}` : ""}
              </span>
            </li>
          ))}
        </ol>
      </div>
    </details>
  );
}

function ProposeDialog({
  onClose,
  dataset,
  body,
  baseVersion,
  changes,
  overrides,
}: {
  onClose: () => void;
  dataset: Dataset;
  body: ReturnType<typeof formToBody>;
  baseVersion: number | null;
  changes: number;
  overrides: ShareOverride[];
}) {
  const submit = useSubmitProposal();
  const toast = useToast();
  const [rationale, setRationale] = useState("");
  const id = useId();
  return (
    <Modal
      open
      onClose={onClose}
      title="Propose this policy"
      description={`${pluralize(changes, "change")} to ${dataset.name}. Nothing changes until an ArchiveAdmin approves it.`}
      footer={
        <>
          <button type="button" className="btn" onClick={onClose}>
            Cancel
          </button>
          <button
            type="button"
            className="btn btn-primary"
            disabled={!rationale.trim() || submit.isPending}
            onClick={() =>
              submit.mutate(
                {
                  dataset_id: dataset.dataset_id,
                  body,
                  rationale: rationale.trim(),
                  base_version: baseVersion,
                  ...(overrides.length ? { share_overrides: overrides } : {}),
                },
                {
                  onSuccess: () => {
                    toast("Proposal sent to the ArchiveAdmins.");
                    onClose();
                  },
                },
              )
            }
          >
            {submit.isPending ? "Sending…" : "Send proposal"}
          </button>
        </>
      }
    >
      <div className="field">
        <label className="field-label" htmlFor={id}>
          Why this change?
        </label>
        <textarea
          id={id}
          value={rationale}
          maxLength={4000}
          onChange={(e) => setRationale(e.target.value)}
        />
        <span className="field-hint">The approver sees this next to the diff and the preview.</span>
      </div>
      {submit.isError ? <ErrorCallout error={submit.error} /> : null}
    </Modal>
  );
}
