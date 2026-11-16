import type { Check } from "../api/types";
import { formatMoney } from "../lib/format";
import { AlertIcon, ErrorIcon, InfoIcon } from "./icons";

const ORDER = { error: 0, warning: 1, info: 2 } as const;

/** Server checks, worst first. Messages are the server's own plain language. */
export function ChecksList({
  checks,
  onGo,
  label = "Checks",
  shareName,
}: {
  checks: Check[];
  onGo?: (check: Check) => void;
  label?: string;
  shareName?: (shareId: string) => string | undefined;
}) {
  if (checks.length === 0) {
    return <p className="muted">No problems found.</p>;
  }
  const sorted = [...checks].sort((a, b) => ORDER[a.severity] - ORDER[b.severity]);
  return (
    <ul className="checks" aria-label={label}>
      {sorted.map((check, i) => (
        <li
          key={`${check.code}-${check.share_id ?? ""}-${i}`}
          className="check"
          data-severity={check.severity}
          data-code={check.code}
        >
          {check.severity === "error" ? (
            <ErrorIcon />
          ) : check.severity === "warning" ? (
            <AlertIcon />
          ) : (
            <InfoIcon />
          )}
          <span>
            <span className="sr-only">{check.severity}: </span>
            {check.share_id && shareName?.(check.share_id) ? (
              <strong>{shareName(check.share_id)}: </strong>
            ) : null}
            {check.message}
          </span>
          {check.fee ? (
            <span className="check-fee num" title="Estimated fee">
              {formatMoney(check.fee)}
            </span>
          ) : (
            <span />
          )}
          {onGo && check.field ? (
            <button type="button" className="btn-link check-go" onClick={() => onGo(check)}>
              Go to the setting
            </button>
          ) : null}
        </li>
      ))}
    </ul>
  );
}

export function severityCounts(checks: Check[]) {
  return {
    error: checks.filter((c) => c.severity === "error").length,
    warning: checks.filter((c) => c.severity === "warning").length,
    info: checks.filter((c) => c.severity === "info").length,
  };
}
