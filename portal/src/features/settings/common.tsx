import { useState, type ReactNode } from "react";
import { Callout, ErrorCallout } from "../../components/Feedback";
import { formatDateTime } from "../../lib/format";
import { useSettingsTest, type SettingCheck, type SettingsTest } from "./queries";

/** status fields are open enums: an unknown value is shown as is. */
const STATUS: Record<string, { label: string; badge: string }> = {
  ok: { label: "OK", badge: "badge-ok" },
  passed: { label: "Passed", badge: "badge-ok" },
  granted: { label: "Granted", badge: "badge-ok" },
  enabled: { label: "Enabled", badge: "badge-ok" },
  warning: { label: "Warning", badge: "badge-warn" },
  attention: { label: "Needs attention", badge: "badge-warn" },
  disabled: { label: "Disabled", badge: "badge-warn" },
  failed: { label: "Failed", badge: "badge-danger" },
  firing: { label: "Firing", badge: "badge-danger" },
  missing: { label: "Missing", badge: "badge-danger" },
  revoked: { label: "Revoked", badge: "badge-danger" },
  unknown: { label: "Unknown", badge: "badge-outline" },
  idle: { label: "No recent cards", badge: "badge-outline" },
  pending: { label: "Running", badge: "badge-info" },
  not_configured: { label: "Not configured", badge: "badge-outline" },
  done: { label: "Done", badge: "badge-ok" },
  todo: { label: "To do", badge: "badge-outline" },
  active: { label: "Active", badge: "badge-ok" },
  planned: { label: "Planned", badge: "badge-info" },
  missing_identity: { label: "Identity unknown", badge: "badge-danger" },
  removed: { label: "Removed", badge: "badge-outline" },
  running: { label: "Running", badge: "badge-info" },
  capped: { label: "Stopped at a cap", badge: "badge-warn" },
};

export function StatusBadge({ status }: { status: string }) {
  const s = STATUS[status] ?? { label: status, badge: "badge-info" };
  return <span className={`badge ${s.badge}`}>{s.label}</span>;
}

export function CheckList({ checks, label }: { checks: SettingCheck[]; label: string }) {
  if (checks.length === 0) return <p className="muted">Nothing to check.</p>;
  return (
    <ul className="stack" aria-label={label} style={{ listStyle: "none", padding: 0 }}>
      {checks.map((c) => (
        <li key={c.code} className="card card-pad">
          <div className="row" style={{ gap: 8 }}>
            <StatusBadge status={c.status} />
            <strong>{c.title}</strong>
          </div>
          <p>{c.message}</p>
          {c.fix ? <p className="muted">What to do: {c.fix}</p> : null}
        </li>
      ))}
    </ul>
  );
}

/** A plain-language step with its status (done, to do, needs attention). */
export interface StepItem {
  code: string;
  title: string;
  status: string;
  detail: ReactNode;
}

export function StepList({ steps, label }: { steps: StepItem[]; label: string }) {
  return (
    <ol className="stack" aria-label={label} style={{ listStyle: "none", padding: 0, margin: 0 }}>
      {steps.map((s) => (
        <li key={s.code} className="card card-pad">
          <div className="row" style={{ gap: 8 }}>
            <StatusBadge status={s.status} />
            <strong>{s.title}</strong>
          </div>
          <div className="muted">{s.detail}</div>
        </li>
      ))}
    </ol>
  );
}

const URL_IN_TEXT = /(https:\/\/[^\s)]+[^\s).,;:])/g;

/** A server's plain-language line, with any https link in it made clickable. */
export function Linkified({ text }: { text: string }) {
  const parts = text.split(URL_IN_TEXT);
  return (
    <>
      {parts.map((part, i) =>
        i % 2 === 1 ? (
          <a key={i} href={part} target="_blank" rel="noopener noreferrer">
            {part}
          </a>
        ) : (
          part
        ),
      )}
    </>
  );
}

export function Fact({ label, children }: { label: string; children: ReactNode }) {
  return (
    <>
      <dt>{label}</dt>
      <dd>{children ?? "—"}</dd>
    </>
  );
}

/** Shows a test the worker runs, polled while pending. */
export function TestOutcome({ testId }: { testId: string | null }) {
  const t = useSettingsTest(testId);
  if (!testId) return null;
  if (t.isError) return <ErrorCallout error={t.error} title="The test couldn't be read" />;
  const test: SettingsTest | undefined = t.data;
  if (!test || test.state === "pending") {
    const stale = test && t.dataUpdatedAt - Date.parse(test.created_at) >= 30_000;
    return (
      <Callout role="status" title={stale ? "Still waiting for the worker" : "Running the test…"}>
        {stale
          ? "The worker hasn't picked the test up in 30 seconds. Check that the worker is running and configured for this."
          : "The worker runs it with the service's own identity."}
      </Callout>
    );
  }
  return (
    <Callout
      role="status"
      tone={test.state === "passed" ? "ok" : "danger"}
      title={test.state === "passed" ? "Test passed" : "Test failed"}
    >
      <p>{test.detail ?? ""}</p>
      {Object.entries(test.result).map(([k, v]) => (
        <p key={k} className="muted">
          {k.replace(/_/g, " ")}: <span className="mono">{String(v)}</span>
        </p>
      ))}
      {test.finished_at ? <p className="muted">At {formatDateTime(test.finished_at)}</p> : null}
    </Callout>
  );
}

export function CopyButton({ text, label = "Copy" }: { text: string; label?: string }) {
  const [copied, setCopied] = useState(false);
  return (
    <button
      type="button"
      className="btn btn-sm"
      onClick={() => {
        if (!("clipboard" in navigator)) return;
        void navigator.clipboard
          .writeText(text)
          .then(() => setCopied(true))
          .catch(() => undefined);
      }}
    >
      {copied ? "Copied" : label}
    </button>
  );
}
