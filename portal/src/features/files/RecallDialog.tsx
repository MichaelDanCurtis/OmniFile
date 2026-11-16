import { useId, useState } from "react";
import { Link, useNavigate } from "react-router";
import { useRecallOptions, useRequestRecall } from "../../api/queries";
import type { RecallOption } from "../../api/types";
import { Callout, ErrorCallout, Loading } from "../../components/Feedback";
import { Modal } from "../../components/Modal";
import { useToast } from "../../components/Toasts";
import {
  ETA_CONFIDENCE_LABEL,
  formatBytes,
  formatDateTime,
  formatMoney,
  formatRelative,
  fromLocalInput,
  toLocalInput,
  priorityLabel,
} from "../../lib/format";

function etaText(option: RecallOption, noPromiseBytes: number | null | undefined): string {
  if (option.eta) {
    return `Ready by ${formatDateTime(option.eta)} (${formatRelative(option.eta)}, ${ETA_CONFIDENCE_LABEL[option.eta_confidence ?? "none"]})`;
  }
  return noPromiseBytes
    ? `No time promised for files over ${formatBytes(noPromiseBytes)}`
    : "No time promised";
}

/** Recall ahead of time: choose a priority, see each one's ETA and fee, then go. */
export function RecallDialog({
  open,
  onClose,
  fileId,
  fileName,
  versionId,
}: {
  open: boolean;
  onClose: () => void;
  fileId: string;
  fileName: string;
  versionId?: string;
}) {
  const options = useRecallOptions(open ? fileId : null, versionId);
  const request = useRequestRecall();
  const navigate = useNavigate();
  const toast = useToast();
  // Any offered priority can be asked for (RecallRequest.priority is open);
  // until the user picks one, use the first offered, not a fixed "standard".
  const [picked, setPicked] = useState<string | null>(null);
  const [dueBy, setDueBy] = useState("");
  const dueId = useId();

  const data = options.data;
  const firstOffered = data?.options.find((o) => o.offered)?.priority ?? null;
  const priority =
    picked && data?.options.some((o) => o.offered && o.priority === picked) ? picked : firstOffered;
  const canRecall = !!data && data.needs_restore && !!priority;

  async function submit() {
    if (!priority) return;
    const recall = await request.mutateAsync({
      file_id: fileId,
      ...(versionId ? { version_id: versionId } : {}),
      priority,
      ...(fromLocalInput(dueBy) ? { due_by: fromLocalInput(dueBy) ?? undefined } : {}),
    });
    onClose();
    toast(
      recall.joined
        ? `You joined a recall already under way for ${fileName}${recall.requesters ? ` (${recall.requesters.count} people waiting)` : ""}.`
        : `Recall started for ${fileName}.`,
    );
    void navigate(`/recalls/${recall.recall_id}`);
  }

  return (
    <Modal
      open={open}
      onClose={onClose}
      title={`Recall ${fileName}`}
      description="Bring the file back from Archive before you need it. You'll get a Teams message when it's ready."
      footer={
        <>
          <button type="button" className="btn" onClick={onClose}>
            Cancel
          </button>
          <button
            type="button"
            className="btn btn-primary"
            disabled={!canRecall || request.isPending}
            onClick={() => void submit().catch(() => undefined)}
          >
            {request.isPending
              ? "Starting…"
              : data?.active_recall
                ? "Join the recall"
                : "Recall file"}
          </button>
        </>
      }
    >
      {options.isPending ? (
        <Loading label="Asking how long it will take…" />
      ) : options.isError ? (
        <ErrorCallout error={options.error} />
      ) : !data?.needs_restore ? (
        <Callout tone="ok" title="No recall needed">
          This file is online. Open it from the share as usual.
        </Callout>
      ) : (
        <>
          {data.active_recall ? (
            <Callout title="A recall is already under way">
              Someone already asked for this file. Recalling it again joins that recall, and you'll
              be told too.{" "}
              <Link to={`/recalls/${data.active_recall.recall_id}`}>See its progress</Link>.
            </Callout>
          ) : null}
          <fieldset className="options">
            <legend className="field-label">How fast?</legend>
            {data.options.map((option) => (
              <label key={option.priority} className="option">
                <input
                  type="radio"
                  name="recall-priority"
                  value={option.priority}
                  checked={priority === option.priority}
                  disabled={!option.offered}
                  onChange={() => setPicked(option.priority)}
                />
                <span className="option-title">
                  {priorityLabel(option.priority)}
                  {option.fee_estimate ? (
                    <span className="muted num"> · about {formatMoney(option.fee_estimate)}</span>
                  ) : null}
                </span>
                <span className="option-body">
                  {option.offered
                    ? etaText(option, data.no_promise_above_bytes)
                    : (option.reason ?? "Not offered for this file.")}
                </span>
              </label>
            ))}
          </fieldset>
          {data.no_promise_above_bytes && data.size > data.no_promise_above_bytes ? (
            <Callout tone="warn" title={`This file is ${formatBytes(data.size)}`}>
              Above {formatBytes(data.no_promise_above_bytes)} the storage provider promises no
              time, so we don't either. The recall view shows each step as it happens.
            </Callout>
          ) : null}
          <div className="field">
            <label className="field-label" htmlFor={dueId}>
              Needed by <span className="muted">(optional)</span>
            </label>
            <input
              id={dueId}
              type="datetime-local"
              value={dueBy}
              min={toLocalInput(new Date())}
              onChange={(e) => setDueBy(e.target.value)}
            />
            <span className="field-hint">Helps your admins plan; it doesn't change the speed.</span>
          </div>
          {request.isError ? <ErrorCallout error={request.error} /> : null}
        </>
      )}
    </Modal>
  );
}
