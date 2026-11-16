import { useId, useState } from "react";
import { ErrorCallout, Loading } from "../../components/Feedback";
import { Modal } from "../../components/Modal";
import { formatBytes } from "../../lib/format";
import type { AnnexFile } from "./api";
import {
  CONFLICT_LABEL,
  replaceSummary,
  usePlacement,
  useRequestPlacement,
  useReturnTargets,
  type ReturnTarget,
} from "./returnsApi";

const STATE_LABEL: Record<string, string> = {
  requested: "Asked for",
  rehydrating: "Coming back from Archive",
  ready: "Ready to be placed",
  writing: "Being written",
  verifying: "Being checked",
  securing: "Getting its permissions",
  placing: "Being put in place",
  placed: "Placed",
  failed: "Failed",
  cancelled: "Cancelled",
};

/** Files a person can ask to have placed back: removed from the NAS, or lost by it. */
export function canGetBack(f: AnnexFile): boolean {
  return f.nas_state === "removed" || f.nas_state === "gone";
}

/** "Get it back" (annex.md 4.6): pick one of the places the annex's return rules
 * allow, each checked against the requester's own right to add a file there. */
export function GetBack({ file }: { file: AnnexFile }) {
  const [open, setOpen] = useState(false);
  const [pick, setPick] = useState<number | null>(null);
  const [note, setNote] = useState("");
  const [priority, setPriority] = useState("standard");
  const [placementId, setPlacementId] = useState<string | null>(null);
  const targets = useReturnTargets(open ? file.file_id : null);
  const request = useRequestPlacement();
  const placement = usePlacement(placementId);
  const ids = { note: useId(), priority: useId(), group: useId() };
  const items = targets.data?.items ?? [];
  const chosen: number = pick ?? items.findIndex((t) => t.allowed && t.default);
  const archived = file.tier === "archive";
  const close = () => {
    setOpen(false);
    setPick(null);
    setPlacementId(null);
    request.reset();
  };

  function submit() {
    const t: ReturnTarget | undefined = items[chosen];
    if (!t || !t.allowed) return;
    request.mutate(
      {
        file_id: file.file_id,
        place_at: t.default ? "default" : t.location,
        ...(note ? { note } : {}),
        ...(archived ? { priority } : {}),
      },
      { onSuccess: (p) => setPlacementId(p.placement_id) },
    );
  }

  const p = placement.data ?? request.data;
  return (
    <>
      <button type="button" className="btn btn-sm" onClick={() => setOpen(true)}>
        Get {file.path} back
      </button>
      <Modal
        open={open}
        onClose={close}
        title="Get it back"
        description={<span className="mono">{file.path}</span>}
        footer={
          p ? (
            <button type="button" className="btn" onClick={close}>
              Close
            </button>
          ) : (
            <>
              <button type="button" className="btn" onClick={close}>
                Cancel
              </button>
              <button
                type="button"
                className="btn btn-primary"
                disabled={request.isPending || chosen < 0 || !items[chosen]?.allowed}
                onClick={submit}
              >
                Place it there
              </button>
            </>
          )
        }
      >
        {p ? (
          <div aria-live="polite">
            <p>
              <span className="badge">{STATE_LABEL[p.state] ?? p.state}</span>{" "}
              {p.display ?? p.target.display}
            </p>
            {p.state === "ready" ? (
              <p className="field-hint">
                It waits here until its writer places it. You&apos;ll be told in Teams when it
                lands.
              </p>
            ) : null}
            {replaceSummary(p) ? (
              <p
                className={
                  p.replace?.state === "kept_aside" ? "callout callout-warn" : "field-hint"
                }
                data-testid="placement-replace"
              >
                {replaceSummary(p)}
              </p>
            ) : null}
            {p.fail_reason ? (
              <p className="callout callout-warn">
                {p.fail_reason}: {p.fail_detail}
              </p>
            ) : null}
          </div>
        ) : targets.isError ? (
          <ErrorCallout error={targets.error} />
        ) : !targets.data ? (
          <Loading />
        ) : items.length === 0 ? (
          <p className="empty">The annex&apos;s return rules place this file nowhere.</p>
        ) : (
          <>
            <fieldset className="field" aria-describedby={ids.group}>
              <legend className="field-label">Where</legend>
              <p id={ids.group} className="field-hint">
                Only places where you could add a file yourself can be picked.
              </p>
              {items.map((t, i) => (
                <label key={`${i}-${t.display ?? ""}`} className="radio">
                  <input
                    type="radio"
                    name={ids.group}
                    checked={chosen === i}
                    disabled={!t.allowed}
                    onChange={() => setPick(i)}
                  />{" "}
                  <span className="mono">{t.display ?? "?"}</span>
                  {t.default ? <span className="badge badge-outline"> default</span> : null}
                  <span className="muted micro">
                    {" "}
                    {CONFLICT_LABEL[t.on_conflict] ?? t.on_conflict}
                    {t.hold_days ? ` · held ${t.hold_days} days` : ""}
                    {t.name_taken ? " · the name is taken now" : ""}
                    {t.free_bytes !== null && t.free_bytes !== undefined
                      ? ` · ${formatBytes(t.free_bytes)} free`
                      : ""}
                  </span>
                  {!t.allowed ? <span className="muted micro"> ({t.why_not})</span> : null}
                </label>
              ))}
            </fieldset>
            {archived ? (
              <div className="field">
                <label className="field-label" htmlFor={ids.priority}>
                  It is in Archive: how fast
                </label>
                <select
                  id={ids.priority}
                  value={priority}
                  onChange={(e) => setPriority(e.target.value)}
                >
                  <option value="standard">Standard (up to 15 hours)</option>
                  <option value="high">High</option>
                </select>
              </div>
            ) : null}
            <div className="field">
              <label className="field-label" htmlFor={ids.note}>
                Why do you need it? (optional)
              </label>
              <textarea
                id={ids.note}
                value={note}
                maxLength={500}
                onChange={(e) => setNote(e.target.value)}
              />
            </div>
            {request.isError ? <ErrorCallout error={request.error} /> : null}
          </>
        )}
      </Modal>
    </>
  );
}
