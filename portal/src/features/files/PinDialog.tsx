import { useId, useState } from "react";
import { usePinPreview, useCreatePins } from "../../api/queries";
import type { PinTier, Share, Skipped } from "../../api/types";
import { useSession } from "../../app/session";
import { ChecksList } from "../../components/Checks";
import { Callout, ErrorCallout } from "../../components/Feedback";
import { Modal } from "../../components/Modal";
import { TierChip } from "../../components/TierChip";
import { useToast } from "../../components/Toasts";
import {
  tierLabel,
  formatMoney,
  fromLocalInput,
  pluralize,
  toLocalInput,
  addDays,
} from "../../lib/format";
import { canOverrideArchive, canSeePrices } from "../../lib/permissions";
import { useDebouncedJson } from "../../lib/useDebounced";

const TIERS: { tier: PinTier; body: string }[] = [
  { tier: "hot", body: "Fastest reads, highest storage price." },
  { tier: "warm", body: "Cheaper storage; Azure bills at least 30 days." },
  { tier: "cold", body: "Cheaper still; Azure bills at least 90 days." },
  { tier: "archive", body: "Cheapest; reads need a recall that takes hours." },
];

export interface PinTarget {
  file_id: string;
  name: string;
}

/**
 * Pin files or folders to a tier (decision 29). The server's warnings (fees,
 * leaving Smart, recall time, monthly cost) are shown before Apply, and any
 * warning has to be acknowledged.
 */
export function PinDialog({
  open,
  onClose,
  items,
  share,
}: {
  open: boolean;
  onClose: () => void;
  items: PinTarget[];
  share: Share;
}) {
  const { me } = useSession();
  const toast = useToast();
  const create = useCreatePins();
  const [tier, setTier] = useState<PinTier>("hot");
  const [untilMode, setUntilMode] = useState<"release" | "date">("date");
  const [until, setUntil] = useState(() => toLocalInput(addDays(new Date(), 30)));
  const [override, setOverride] = useState(false);
  const [reason, setReason] = useState("");
  const [ack, setAck] = useState(false);
  const [skipped, setSkipped] = useState<Skipped[] | null>(null);
  const reasonId = useId();
  const untilId = useId();

  const untilIso = untilMode === "date" ? fromLocalInput(until) : null;
  const request = {
    items: items.map((i) => i.file_id),
    tier,
    ...(untilIso ? { until: untilIso } : {}),
    ...(override ? { override_archive: true } : {}),
  };
  const debounced = useDebouncedJson(request, 250);
  const preview = usePinPreview(open ? debounced : null);
  const current =
    JSON.stringify(request) === JSON.stringify(debounced) && !preview.isPlaceholderData;
  const checks = preview.data?.items.flatMap((i) => i.checks) ?? [];
  const warnings = checks.filter((c) => c.severity !== "info");
  const errors = checks.filter((c) => c.severity === "error");
  const ownerOverride = canOverrideArchive(me, share) && tier !== "archive";
  const ready =
    current &&
    !!preview.data &&
    errors.length === 0 &&
    (warnings.length === 0 || ack) &&
    reason.trim().length > 0 &&
    (untilMode === "release" || !!untilIso);

  async function apply() {
    const result = await create.mutateAsync({ ...request, reason: reason.trim() });
    if (result.skipped.length > 0) {
      setSkipped(result.skipped);
      return;
    }
    toast(`Pinned ${pluralize(result.pins.length, "item")} to ${tierLabel(tier)}.`);
    onClose();
  }

  const totals = preview.data?.totals;
  return (
    <Modal
      open={open}
      onClose={onClose}
      wide
      title={`Pin ${items.length === 1 ? (items[0]?.name ?? "item") : pluralize(items.length, "item")} to a tier`}
      description="A pin overrides the policy schedule until it ends. Then the policy takes over again."
      footer={
        skipped ? (
          <button type="button" className="btn btn-primary" onClick={onClose}>
            Done
          </button>
        ) : (
          <>
            <button type="button" className="btn" onClick={onClose}>
              Cancel
            </button>
            <button
              type="button"
              className="btn btn-primary"
              disabled={!ready || create.isPending}
              onClick={() => void apply().catch(() => undefined)}
            >
              {create.isPending ? "Applying…" : `Apply pin`}
            </button>
          </>
        )
      }
    >
      {skipped ? (
        <Callout tone="warn" title={`${pluralize(skipped.length, "item")} couldn't be pinned`}>
          <ul>
            {skipped.map((s) => (
              <li key={s.file_id}>{s.message}</li>
            ))}
          </ul>
          <p>The rest were pinned.</p>
        </Callout>
      ) : (
        <>
          <fieldset className="options options-row">
            <legend className="field-label">Tier</legend>
            {TIERS.map((t) => (
              <label key={t.tier} className="option">
                <input
                  type="radio"
                  name="pin-tier"
                  value={t.tier}
                  checked={tier === t.tier}
                  onChange={() => {
                    setTier(t.tier);
                    setAck(false);
                  }}
                />
                <span className="option-title">
                  <TierChip tier={t.tier} />
                </span>
                <span className="option-body">{t.body}</span>
              </label>
            ))}
          </fieldset>

          <fieldset className="options">
            <legend className="field-label">For how long?</legend>
            <label className="check-row">
              <input
                type="radio"
                name="pin-until"
                checked={untilMode === "date"}
                onChange={() => setUntilMode("date")}
              />
              <span>Until a date</span>
            </label>
            {untilMode === "date" ? (
              <div className="field" style={{ paddingLeft: 26 }}>
                <label className="sr-only" htmlFor={untilId}>
                  Pin ends
                </label>
                <input
                  id={untilId}
                  type="datetime-local"
                  value={until}
                  min={toLocalInput(new Date())}
                  onChange={(e) => setUntil(e.target.value)}
                />
              </div>
            ) : null}
            <label className="check-row">
              <input
                type="radio"
                name="pin-until"
                checked={untilMode === "release"}
                onChange={() => setUntilMode("release")}
              />
              <span>Until someone releases it</span>
            </label>
          </fieldset>

          {ownerOverride ? (
            <label className="check-row">
              <input
                type="checkbox"
                checked={override}
                onChange={(e) => {
                  setOverride(e.target.checked);
                  setAck(false);
                }}
              />
              <span>
                Keep it {tierLabel(tier)} past its archive date
                <span className="field-hint">
                  Owners only. Without this, the pin ends on the archive date.
                </span>
              </span>
            </label>
          ) : null}

          <div className="field">
            <label className="field-label" htmlFor={reasonId}>
              Why?
            </label>
            <textarea
              id={reasonId}
              value={reason}
              maxLength={500}
              required
              onChange={(e) => setReason(e.target.value)}
              placeholder="For example: reprocessing for the 2026 velocity model"
            />
            <span className="field-hint">Recorded in the audit log with your name.</span>
          </div>

          <section
            className={preview.isFetching ? "stack updating" : "stack"}
            aria-busy={preview.isFetching}
          >
            <h3>Before you apply</h3>
            {preview.isError ? (
              <ErrorCallout error={preview.error} />
            ) : !preview.data ? (
              <p className="muted">Working out fees and warnings…</p>
            ) : (
              <>
                <dl className="kv">
                  <dt>Files affected</dt>
                  <dd className="num">{totals?.files ?? items.length}</dd>
                  {canSeePrices(me) ? (
                    <>
                      <dt>One-off fees</dt>
                      <dd className="num">{formatMoney(totals?.fee)}</dd>
                      <dt>Storage cost change</dt>
                      <dd className="num">
                        {totals?.monthly_cost_change
                          ? `${Number(totals.monthly_cost_change.amount) > 0 ? "+" : ""}${formatMoney(totals.monthly_cost_change)} per month`
                          : "—"}
                      </dd>
                    </>
                  ) : null}
                </dl>
                <ChecksList checks={checks} label="Pin warnings" />
                {warnings.length > 0 && errors.length === 0 ? (
                  <label className="check-row">
                    <input
                      type="checkbox"
                      checked={ack}
                      onChange={(e) => setAck(e.target.checked)}
                    />
                    <span>I've read the warnings above</span>
                  </label>
                ) : null}
              </>
            )}
          </section>
          {create.isError ? <ErrorCallout error={create.error} /> : null}
        </>
      )}
    </Modal>
  );
}
