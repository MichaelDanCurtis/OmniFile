import { useId, useState } from "react";
import { useCreateHolds } from "../../api/queries";
import type { Skipped } from "../../api/types";
import { authIsRecent, useSession } from "../../app/session";
import { signIn } from "../../api/client";
import { Callout, ErrorCallout } from "../../components/Feedback";
import { Modal } from "../../components/Modal";
import { useToast } from "../../components/Toasts";
import { addDays, fromLocalInput, pluralize, toLocalInput } from "../../lib/format";
import type { PinTarget } from "./PinDialog";

function reportSkipped(skipped: Skipped[], toast: ReturnType<typeof useToast>) {
  if (skipped.length === 0) return;
  toast(
    `${pluralize(skipped.length, "item")} skipped: ${skipped.map((s) => s.message).join(" ")}`,
    "info",
  );
}

/** Decision 33's hold length; the portal offers it as the default. */
export const KEEP_ONLINE_DAYS = 14;

export function KeepOnlineDialog({
  open,
  onClose,
  items,
}: {
  open: boolean;
  onClose: () => void;
  items: PinTarget[];
}) {
  const create = useCreateHolds();
  const toast = useToast();
  const [mode, setMode] = useState<"date" | "release">("date");
  const [until, setUntil] = useState(() => toLocalInput(addDays(new Date(), KEEP_ONLINE_DAYS)));
  const [reason, setReason] = useState("");
  const untilId = useId();
  const reasonId = useId();
  const untilIso = mode === "date" ? fromLocalInput(until) : null;
  const what = items.length === 1 ? (items[0]?.name ?? "item") : pluralize(items.length, "item");

  async function apply() {
    const result = await create.mutateAsync({
      items: items.map((i) => i.file_id),
      kind: "keep_online",
      ...(untilIso ? { until: untilIso } : {}),
      reason: reason.trim(),
    });
    reportSkipped(result.skipped, toast);
    if (result.holds.length > 0) toast(`Keeping ${pluralize(result.holds.length, "item")} online.`);
    onClose();
  }

  return (
    <Modal
      open={open}
      onClose={onClose}
      title={`Keep ${what} online`}
      footer={
        <>
          <button type="button" className="btn" onClick={onClose}>
            Cancel
          </button>
          <button
            type="button"
            className="btn btn-primary"
            disabled={!reason.trim() || (mode === "date" && !untilIso) || create.isPending}
            onClick={() => void apply().catch(() => undefined)}
          >
            {create.isPending ? "Saving…" : "Keep online"}
          </button>
        </>
      }
    >
      <Callout>
        The gateway keeps the whole file on its disks, so it opens without a recall. No cloud tier
        changes and there is no provider fee. When the hold ends, the policy (and the archive date)
        applies again.
      </Callout>
      <fieldset className="options">
        <legend className="field-label">Until</legend>
        <label className="check-row">
          <input
            type="radio"
            name="keep-until"
            checked={mode === "date"}
            onChange={() => setMode("date")}
          />
          <span>A date ({KEEP_ONLINE_DAYS} days from now unless you change it)</span>
        </label>
        {mode === "date" ? (
          <div className="field" style={{ paddingLeft: 26 }}>
            <label className="sr-only" htmlFor={untilId}>
              Keep online until
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
            name="keep-until"
            checked={mode === "release"}
            onChange={() => setMode("release")}
          />
          <span>Until someone releases it</span>
        </label>
      </fieldset>
      <div className="field">
        <label className="field-label" htmlFor={reasonId}>
          Why?
        </label>
        <textarea
          id={reasonId}
          value={reason}
          maxLength={500}
          onChange={(e) => setReason(e.target.value)}
        />
      </div>
      {create.isError ? <ErrorCallout error={create.error} /> : null}
    </Modal>
  );
}

export function LegalHoldDialog({
  open,
  onClose,
  items,
}: {
  open: boolean;
  onClose: () => void;
  items: PinTarget[];
}) {
  const { session } = useSession();
  const create = useCreateHolds();
  const toast = useToast();
  const [reason, setReason] = useState("");
  const reasonId = useId();
  const recent = authIsRecent(session);
  const what = items.length === 1 ? (items[0]?.name ?? "item") : pluralize(items.length, "item");

  async function apply() {
    const result = await create.mutateAsync({
      items: items.map((i) => i.file_id),
      kind: "legal",
      reason: reason.trim(),
    });
    reportSkipped(result.skipped, toast);
    if (result.holds.length > 0)
      toast(`Legal hold set on ${pluralize(result.holds.length, "item")}.`);
    onClose();
  }

  return (
    <Modal
      open={open}
      onClose={onClose}
      title={`Legal hold on ${what}`}
      description="Keeps every version beyond its retention until an ArchiveAdmin clears the hold."
      footer={
        <>
          <button type="button" className="btn" onClick={onClose}>
            Cancel
          </button>
          {recent ? (
            <button
              type="button"
              className="btn btn-primary"
              disabled={!reason.trim() || create.isPending}
              onClick={() => void apply().catch(() => undefined)}
            >
              {create.isPending ? "Setting…" : "Set legal hold"}
            </button>
          ) : (
            <button
              type="button"
              className="btn btn-primary"
              onClick={() => signIn({ reauth: true })}
            >
              Sign in again to continue
            </button>
          )}
        </>
      }
    >
      {!recent ? (
        <Callout tone="warn" title="Sign in again first">
          Legal holds need a sign-in within the last 10 minutes. You'll come back here afterwards.
        </Callout>
      ) : null}
      <div className="field">
        <label className="field-label" htmlFor={reasonId}>
          Matter or reason
        </label>
        <textarea
          id={reasonId}
          value={reason}
          maxLength={500}
          onChange={(e) => setReason(e.target.value)}
          placeholder="For example: Matter 2026-114"
        />
      </div>
      {create.isError ? <ErrorCallout error={create.error} /> : null}
    </Modal>
  );
}
