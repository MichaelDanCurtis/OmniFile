import { useId, useState } from "react";
import { useNewEdgeBundle } from "../../api/queries";
import type { components } from "../../api/schema.gen";
import { Callout, ErrorCallout } from "../../components/Feedback";
import { Modal } from "../../components/Modal";
import { formatDateTime } from "../../lib/format";

type Bundle = components["schemas"]["EnrolmentBundle"];

/** The one-time code, with how to use it. It is never shown again. */
function CodeShown({ issued }: { issued: Bundle }) {
  const codeId = useId();
  const [copied, setCopied] = useState(false);
  return (
    <>
      <Callout tone="warn">
        Copy this code now: it is shown only once. It works once, until{" "}
        {formatDateTime(issued.expires_at)}.
      </Callout>
      <div className="field">
        <label className="field-label" htmlFor={codeId}>
          Enrolment code for {issued.name}
        </label>
        <textarea
          id={codeId}
          className="mono"
          readOnly
          rows={4}
          value={issued.bundle}
          onFocus={(e) => e.currentTarget.select()}
        />
      </div>
      <button
        type="button"
        className="btn"
        onClick={() => {
          // Absent outside a secure context: the text is selectable anyway.
          if (!("clipboard" in navigator)) return;
          void navigator.clipboard
            .writeText(issued.bundle)
            .then(() => setCopied(true))
            .catch(() => undefined);
        }}
      >
        {copied ? "Copied" : "Copy code"}
      </button>
      <p>
        On the new filer&apos;s console, setup asks for this code. If setup has already run, an
        administrator runs <code>sudo omnifile-setup enrol</code> on the filer and pastes it.
      </p>
    </>
  );
}

/** A new code for an edge that hasn't enrolled (lost, or expired). */
export function NewCodeDialog({
  edge,
  onClose,
}: {
  edge: { edge_id: string; name: string } | null;
  onClose: () => void;
}) {
  const issue = useNewEdgeBundle();

  function close() {
    issue.reset();
    onClose();
  }

  return (
    <Modal
      open={edge !== null}
      onClose={close}
      title={`New code for ${edge?.name ?? ""}`}
      footer={
        issue.data ? (
          <button type="button" className="btn btn-primary" onClick={close}>
            Done
          </button>
        ) : (
          <>
            <button type="button" className="btn" onClick={close}>
              Cancel
            </button>
            <button
              type="button"
              className="btn btn-primary"
              disabled={issue.isPending || !edge}
              onClick={() => edge && issue.mutate(edge.edge_id)}
            >
              {issue.isPending ? "Creating…" : "Create a new code"}
            </button>
          </>
        )
      }
    >
      {issue.data ? (
        <CodeShown issued={issue.data} />
      ) : (
        <p>The edge hasn&apos;t enrolled. A new code replaces the old one, which stops working.</p>
      )}
      {issue.isError ? <ErrorCallout error={issue.error} /> : null}
    </Modal>
  );
}
