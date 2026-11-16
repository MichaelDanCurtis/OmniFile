import { useEffect, useRef, useState } from "react";
import { Link } from "react-router";
import { useFile, useHolds, useRecall, useReleaseHold, useReleasePin } from "../../api/queries";
import type { Share } from "../../api/types";
import { useSession } from "../../app/session";
import { Callout, ErrorCallout, Loading } from "../../components/Feedback";
import { ClockIcon, PinIcon, RecallIcon, ShieldIcon, XIcon } from "../../components/icons";
import { TierChip } from "../../components/TierChip";
import { useToast } from "../../components/Toasts";
import {
  ETA_CONFIDENCE_LABEL,
  RECALL_STATE_LABEL,
  tierLabel,
  formatBytes,
  formatDate,
  formatDateTime,
  formatRelative,
} from "../../lib/format";
import { canKeepOnline, canPin, isArchiveAdmin } from "../../lib/permissions";
import { NextStep, StateLabel, needsRecall } from "./EntryBits";
import { KeepOnlineDialog, LegalHoldDialog } from "./HoldDialogs";
import { PinDialog } from "./PinDialog";
import { RecallDialog } from "./RecallDialog";
import { VersionsPanel } from "./Versions";
import { LazyPart, lazyNamed } from "../../app/lazy";

// The journey is its own chunk (app/lazy.tsx).
const FileJourney = lazyNamed(() => import("./visual/FileJourney"), "FileJourney");

type Dialog = "recall" | "pin" | "keep" | "legal" | null;

export function FileDrawer({
  fileId,
  share,
  onClose,
}: {
  fileId: string;
  share: Share;
  onClose: () => void;
}) {
  const { me } = useSession();
  const file = useFile(fileId);
  const [dialog, setDialog] = useState<Dialog>(null);
  const [confirmLegal, setConfirmLegal] = useState<string | null>(null);
  const holds = useHolds({ fileId });
  const releasePin = useReleasePin();
  const releaseHold = useReleaseHold();
  const toast = useToast();
  const recall = useRecall(file.data?.active_recall?.recall_id);
  const heading = useRef<HTMLHeadingElement>(null);
  const loaded = !!file.data;
  // Move focus into the drawer once its content is there.
  useEffect(() => {
    if (loaded) heading.current?.focus();
  }, [loaded]);
  // Esc closes the drawer, unless a dialog is open (Esc closes that first).
  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      if (event.key !== "Escape" || event.defaultPrevented) return;
      if (document.querySelector("dialog[open]")) return;
      onClose();
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [onClose]);

  if (file.isPending) {
    return (
      <aside className="drawer" aria-label="File details">
        <Loading />
      </aside>
    );
  }
  if (file.isError) {
    return (
      <aside className="drawer" aria-label="File details">
        <div className="drawer-head">
          <h2>File</h2>
          <button type="button" className="icon-btn" aria-label="Close details" onClick={onClose}>
            <XIcon />
          </button>
        </div>
        <div className="drawer-section">
          <ErrorCallout error={file.error} />
        </div>
      </aside>
    );
  }

  const f = file.data;
  const target = [{ file_id: f.file_id, name: f.name }];
  // Every hold that applies here, on the item or a folder above it (GET /v1/holds?file_id=),
  // each naming the folder it is set on in `inherited_from`.
  const applying = holds.data?.items ?? [];
  const mine = (id: string | null | undefined) => !!id && id === me.identity_id;
  const archived = needsRecall(f);
  const pinnable = canPin(me, share);
  const keepable = canKeepOnline(me, share);

  return (
    <aside className="drawer" aria-labelledby="drawer-title">
      <div className="drawer-head">
        <div className="stack" style={{ gap: 4 }}>
          <span className="micro">{f.kind === "dir" ? "Folder" : "File"}</span>
          <h2 id="drawer-title" ref={heading} tabIndex={-1}>
            {f.name}
          </h2>
        </div>
        <button type="button" className="icon-btn" aria-label="Close details" onClick={onClose}>
          <XIcon />
        </button>
      </div>

      <section className="drawer-section" aria-label="Where it is">
        <div className="row">
          <TierChip tier={f.tier} />
          <StateLabel entry={f} />
        </div>
        <dl className="kv">
          {f.kind === "file" ? (
            <>
              <dt>Size</dt>
              <dd className="num">{formatBytes(f.size)}</dd>
              <dt>On the gateway</dt>
              <dd className="num">
                {f.state === "resident" ? "The whole file" : formatBytes(f.resident_bytes ?? 0)}
              </dd>
            </>
          ) : null}
          <dt>Next</dt>
          <dd>
            <NextStep entry={f} />
          </dd>
          <dt>Archive date</dt>
          <dd>{formatDate(f.archive_at)}</dd>
          <dt>Last read</dt>
          <dd>{f.last_read_at ? formatRelative(f.last_read_at) : "—"}</dd>
          {f.tier.restore_expires_at ? (
            <>
              <dt>Restored copy until</dt>
              <dd>{formatDateTime(f.tier.restore_expires_at)}</dd>
            </>
          ) : null}
        </dl>
        {f.active_recall ? (
          <Callout title={`Recall: ${RECALL_STATE_LABEL[f.active_recall.state]}`}>
            <p>
              {recall.data?.eta
                ? `Expected ${formatDateTime(recall.data.eta)} (${ETA_CONFIDENCE_LABEL[recall.data.eta_confidence ?? "none"]}).`
                : "No time promised for this file."}{" "}
              <Link to={`/recalls/${f.active_recall.recall_id}`}>See all four steps</Link>
            </p>
          </Callout>
        ) : archived ? (
          <div className="row">
            <button type="button" className="btn btn-primary" onClick={() => setDialog("recall")}>
              <RecallIcon /> Recall ahead of time…
            </button>
          </div>
        ) : f.kind === "file" ? (
          <p className="muted">Online. Open it from the share as usual.</p>
        ) : null}
      </section>

      {f.kind === "file" ? (
        <LazyPart label="Loading where the file is…">
          <FileJourney file={f} />
        </LazyPart>
      ) : null}

      <section className="drawer-section" aria-labelledby="tiering-title">
        <h3 id="tiering-title">Pins and holds</h3>
        {f.pin ? (
          <div className="row">
            <PinIcon />
            <span>
              Pinned to <strong>{tierLabel(f.pin.tier)}</strong>
              {f.pin.until ? ` until ${formatDateTime(f.pin.until)}` : " until released"}
            </span>
            {f.pin.inherited_from ? (
              <FolderLink shareId={share.share_id} folderId={f.pin.inherited_from} />
            ) : null}
            {pinnable && !f.pin.inherited_from ? (
              <button
                type="button"
                className="btn btn-sm btn-ghost"
                disabled={releasePin.isPending}
                onClick={() =>
                  releasePin.mutate(f.pin?.pin_id ?? "", {
                    onSuccess: () => toast("Pin released."),
                  })
                }
              >
                Release
              </button>
            ) : null}
          </div>
        ) : (
          <p className="muted">No pin: the policy schedule applies.</p>
        )}
        {applying.map((hold) => (
          <div key={hold.hold_id} className="row">
            {hold.kind === "legal" ? <ShieldIcon /> : <ClockIcon />}
            <span>
              {hold.kind === "legal" ? "Legal hold" : "Kept online"}
              {hold.until ? ` until ${formatDateTime(hold.until)}` : ""}
              <span className="muted"> · {hold.reason}</span>
            </span>
            {hold.inherited_from ? (
              // Set on a folder: releasing it here would release it for the whole folder.
              <FolderLink shareId={share.share_id} folderId={hold.inherited_from} />
            ) : (hold.kind === "keep_online" && (keepable || mine(hold.set_by?.identity_id))) ||
              (hold.kind === "legal" && isArchiveAdmin(me)) ? (
              <button
                type="button"
                className="btn btn-sm btn-ghost"
                disabled={releaseHold.isPending}
                onClick={() => {
                  if (hold.kind === "legal") setConfirmLegal(hold.hold_id);
                  else
                    releaseHold.mutate(hold.hold_id, { onSuccess: () => toast("Hold released.") });
                }}
              >
                Release
              </button>
            ) : null}
            {confirmLegal === hold.hold_id ? (
              <div
                className="callout callout-warn confirm"
                role="alertdialog"
                aria-labelledby={`confirm-${hold.hold_id}`}
              >
                <ShieldIcon />
                <div className="stack" style={{ gap: 8 }}>
                  <strong id={`confirm-${hold.hold_id}`}>
                    Release the legal hold on {f.name}?
                  </strong>
                  <p>
                    Its versions are then kept only for their normal retention, and older ones may
                    be pruned. This is audited.
                  </p>
                  <div className="row">
                    <button
                      type="button"
                      className="btn btn-sm btn-danger"
                      disabled={releaseHold.isPending}
                      onClick={() =>
                        releaseHold.mutate(hold.hold_id, {
                          onSuccess: () => {
                            setConfirmLegal(null);
                            toast("Legal hold released.");
                          },
                        })
                      }
                    >
                      Release legal hold
                    </button>
                    <button
                      type="button"
                      className="btn btn-sm"
                      onClick={() => setConfirmLegal(null)}
                    >
                      Keep it
                    </button>
                  </div>
                </div>
              </div>
            ) : null}
          </div>
        ))}

        {releasePin.isError ? <ErrorCallout error={releasePin.error} /> : null}
        {releaseHold.isError ? <ErrorCallout error={releaseHold.error} /> : null}
        {pinnable || keepable || isArchiveAdmin(me) ? (
          <div className="row">
            {pinnable ? (
              <button type="button" className="btn btn-sm" onClick={() => setDialog("pin")}>
                <PinIcon /> Pin to a tier…
              </button>
            ) : null}
            {keepable ? (
              <button type="button" className="btn btn-sm" onClick={() => setDialog("keep")}>
                <ClockIcon /> Keep online…
              </button>
            ) : null}
            {isArchiveAdmin(me) ? (
              <button type="button" className="btn btn-sm" onClick={() => setDialog("legal")}>
                <ShieldIcon /> Legal hold…
              </button>
            ) : null}
          </div>
        ) : null}
      </section>

      {f.kind === "file" ? (
        <section className="drawer-section" aria-labelledby="versions-title">
          <h3 id="versions-title">Versions</h3>
          <VersionsPanel fileId={f.file_id} fileName={f.name} share={share} />
        </section>
      ) : null}

      <section className="drawer-section" aria-labelledby="details-title">
        <h3 id="details-title">Details</h3>
        <dl className="kv">
          <dt>Path</dt>
          <dd className="mono">/{f.path.map((p) => p.name ?? "…").join("/")}</dd>
          {f.blake3 ? (
            <>
              <dt>BLAKE3</dt>
              <dd className="mono">{f.blake3}</dd>
            </>
          ) : null}
          {f.crc64nvme ? (
            <>
              <dt>CRC64-NVME</dt>
              <dd className="mono">{f.crc64nvme}</dd>
            </>
          ) : null}
          <dt>Owner</dt>
          <dd className="mono">{f.owner_sid ?? "—"}</dd>
          <dt>uid / gid / mode</dt>
          <dd className="mono">
            {f.uid ?? "—"} / {f.gid ?? "—"} / {f.mode ?? "—"}
          </dd>
          <dt>Written</dt>
          <dd>{formatDateTime(f.written_at)}</dd>
          {f.retention_until ? (
            <>
              <dt>WORM retention</dt>
              <dd>until {formatDate(f.retention_until)}</dd>
            </>
          ) : null}
          <dt>Versions</dt>
          <dd className="num">{f.versions_count ?? "—"}</dd>
        </dl>
      </section>

      {dialog === "recall" ? (
        <RecallDialog open onClose={() => setDialog(null)} fileId={f.file_id} fileName={f.name} />
      ) : null}
      {dialog === "pin" ? (
        <PinDialog open onClose={() => setDialog(null)} items={target} share={share} />
      ) : null}
      {dialog === "keep" ? (
        <KeepOnlineDialog open onClose={() => setDialog(null)} items={target} />
      ) : null}
      {dialog === "legal" ? (
        <LegalHoldDialog open onClose={() => setDialog(null)} items={target} />
      ) : null}
    </aside>
  );
}

/** Pins and holds set on a folder are managed on that folder. */
function FolderLink({ shareId, folderId }: { shareId: string; folderId: string }) {
  return (
    <Link className="btn btn-sm btn-ghost" to={`/files/${shareId}/${folderId}?file=${folderId}`}>
      Set on a folder above: open it
    </Link>
  );
}
