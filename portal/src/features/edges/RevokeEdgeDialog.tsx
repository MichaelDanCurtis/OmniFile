import { useId, useState } from "react";
import { useRevokeEdge } from "../../api/queries";
import { ErrorCallout } from "../../components/Feedback";
import { Modal } from "../../components/Modal";
import { useToast } from "../../components/Toasts";

/**
 * Revokes a lost or stolen filer (ArchiveAdmin, re-auth; portal-api.md 5.15).
 * It stops working at once and can never renew. Its data stays. One-way.
 */
export function RevokeEdgeDialog({
  edge,
  onClose,
}: {
  edge: { edge_id: string; name: string } | null;
  onClose: () => void;
}) {
  const revoke = useRevokeEdge();
  const toast = useToast();
  const [typed, setTyped] = useState("");
  const nameId = useId();
  const matches = edge !== null && typed === edge.name;

  function close() {
    revoke.reset();
    setTyped("");
    onClose();
  }

  return (
    <Modal
      open={edge !== null}
      onClose={close}
      title={`Revoke ${edge?.name ?? ""}?`}
      footer={
        <>
          <button type="button" className="btn" onClick={close}>
            Cancel
          </button>
          <button
            type="button"
            className="btn btn-danger"
            disabled={!matches || revoke.isPending}
            onClick={() =>
              edge &&
              revoke.mutate(edge.edge_id, {
                onSuccess: () => {
                  toast(`${edge.name} was revoked. Its data is kept.`);
                  close();
                },
              })
            }
          >
            {revoke.isPending ? "Revoking…" : "Revoke edge"}
          </button>
        </>
      }
    >
      <div className="stack">
        <p>
          <strong>For a filer that is lost or stolen.</strong> It stops working at once: the service
          refuses it, it can&apos;t renew its certificate, and an unused code stops working. People
          on its shares lose access until another filer serves them.
        </p>
        <p>
          <strong>Nothing is deleted.</strong> Its datasets, shares and files stay in the catalog
          and in storage.
        </p>
        <p>
          <strong>It can&apos;t be undone.</strong> A filer that turns up again is set up and added
          as a new edge.
        </p>
        <div className="field">
          <label className="field-label" htmlFor={nameId}>
            Type the edge name, <span className="mono">{edge?.name}</span>, to confirm
          </label>
          <input
            id={nameId}
            type="text"
            autoComplete="off"
            spellCheck={false}
            value={typed}
            aria-invalid={typed.length > 0 && !matches}
            onChange={(e) => setTyped(e.target.value)}
          />
        </div>
        {revoke.isError ? (
          <ErrorCallout error={revoke.error} title="The edge wasn't revoked" />
        ) : null}
      </div>
    </Modal>
  );
}
