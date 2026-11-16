import type { Recall } from "../api/types";
import { formatDateTime } from "../lib/format";
import { CheckIcon, XIcon } from "./icons";

const NAMES = {
  requested: "Requested",
  rehydrating: "Rehydrating",
  downloading: "Downloading",
  available: "Available",
} as const;

const HINTS = {
  requested: "Queued with the storage provider",
  rehydrating: "The provider brings it out of Archive",
  downloading: "Copying back to the gateway",
  available: "Open it as usual",
} as const;

/** The four-step recall view (AGENTS.md "the file is archived" experience). */
export function RecallSteps({ recall }: { recall: Recall }) {
  const current = recall.steps.find((s) => s.status === "current" || s.status === "failed");
  return (
    <div className="stack">
      <ol className="steps" aria-label="Recall progress">
        {recall.steps.map((step, i) => (
          <li
            key={step.step}
            className="step"
            data-status={step.status}
            aria-current={step.status === "current" ? "step" : undefined}
          >
            <span className="step-dot" aria-hidden="true">
              {step.status === "done" ? (
                <CheckIcon />
              ) : step.status === "failed" ? (
                <XIcon />
              ) : (
                i + 1
              )}
            </span>
            <span>
              <span className="step-name">{NAMES[step.step]}</span>
              <span className="sr-only">
                {" "}
                —{" "}
                {step.status === "done"
                  ? "done"
                  : step.status === "current"
                    ? "in progress"
                    : step.status === "failed"
                      ? "failed"
                      : "not yet"}
              </span>
            </span>
            <span className="step-at">
              {step.at
                ? formatDateTime(step.at)
                : step.status === "current"
                  ? HINTS[step.step]
                  : ""}
            </span>
          </li>
        ))}
      </ol>
      <p className="sr-only" aria-live="polite">
        {recall.state === "available"
          ? "The file is available."
          : recall.state === "failed"
            ? "The recall failed."
            : recall.state === "superseded"
              ? (recall.superseded?.text ?? "The recall stopped: the file changed.")
              : current
                ? `Now: ${NAMES[current.step]}.`
                : ""}
      </p>
    </div>
  );
}
