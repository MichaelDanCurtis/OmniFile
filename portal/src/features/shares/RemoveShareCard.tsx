import { useId, useState } from "react";
import { useNavigate } from "react-router";
import { useRemoveShare } from "../../api/queries";
import type { Share } from "../../api/types";
import { ErrorCallout } from "../../components/Feedback";
import { ShareIcon } from "../../components/icons";
import { Modal } from "../../components/Modal";
import { useToast } from "../../components/Toasts";

/**
 * Removes a share (ArchiveAdmin; portal-api.md 5.16). Only the share goes: its
 * dataset, files and versions stay. A WORM share is never removed.
 */
export function RemoveShareCard({ share }: { share: Share }) {
  const [confirming, setConfirming] = useState(false);
  const remove = useRemoveShare(share.share_id);
  const navigate = useNavigate();
  const toast = useToast();
  const wormId = useId();
  const worm = share.worm.enabled;
  const dataset = share.dataset_name ?? "its dataset";

  function close() {
    remove.reset();
    setConfirming(false);
  }

  return (
    <section className="card danger-zone" aria-labelledby="remove-share-title">
      <div className="card-head">
        <ShareIcon />
        <h2 id="remove-share-title">Remove this share</h2>
      </div>
      <div className="card-body stack">
        <p>
          People stop reaching the data through {share.name}. Nothing is deleted: the dataset{" "}
          {dataset}, its files and their versions stay.
        </p>
        <div className="row">
          <button
            type="button"
            className="btn btn-danger"
            disabled={worm}
            aria-describedby={worm ? wormId : undefined}
            onClick={() => setConfirming(true)}
          >
            Remove share…
          </button>
        </div>
        {worm ? (
          <p id={wormId} className="field-hint">
            A WORM share can&apos;t be removed: it keeps serving its files while their retention
            runs.
          </p>
        ) : null}
      </div>
      <Modal
        open={confirming}
        onClose={close}
        title={`Remove ${share.name}?`}
        footer={
          <>
            <button type="button" className="btn" onClick={close}>
              Cancel
            </button>
            <button
              type="button"
              className="btn btn-danger"
              disabled={remove.isPending}
              onClick={() =>
                remove.mutate(
                  { etag: share.etag },
                  {
                    onSuccess: () => {
                      toast(`${share.name} was removed. Its data is kept in ${dataset}.`);
                      void navigate("/shares");
                    },
                  },
                )
              }
            >
              {remove.isPending ? "Removing…" : "Remove share"}
            </button>
          </>
        }
      >
        <div className="stack">
          <p>
            <strong>People lose access through this share.</strong> Its Windows and NFS paths stop
            working within a minute.
          </p>
          <p>
            <strong>Nothing is deleted.</strong> The dataset {dataset}, its files and their versions
            stay, and a new share can serve them again.
          </p>
          {remove.isError ? (
            <ErrorCallout error={remove.error} title="The share wasn't removed" />
          ) : null}
        </div>
      </Modal>
    </section>
  );
}
