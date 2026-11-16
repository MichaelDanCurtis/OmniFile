import { useEffect, useState, type ReactNode } from "react";
import { ApiError } from "../../api/client";
import { Link } from "react-router";
import { useRestoreVersion, useVersionRestore, useVersions } from "../../api/queries";
import type { Share, Version } from "../../api/types";
import { useSession } from "../../app/session";
import { Callout, ErrorCallout, Loading } from "../../components/Feedback";
import { DownloadIcon, RewindIcon, ShieldIcon } from "../../components/icons";
import { Modal } from "../../components/Modal";
import { TierChip } from "../../components/TierChip";
import { useToast } from "../../components/Toasts";
import { formatBytes, formatDate, formatDateTime } from "../../lib/format";
import { canRestoreVersion } from "../../lib/permissions";
import { restoredName, restoredTimestampedName, type RestoreTarget } from "../../lib/restoreNames";
import { RecallDialog } from "./RecallDialog";
import { isSafeDownloadUrl, useDownload } from "./useDownload";

const KIND_LABEL: Record<Version["kind"], string> = {
  content: "Content changed",
  metadata: "Details changed",
  rename: "Renamed or moved",
  tombstone: "Deleted",
};

/** Rewind for one file: its versions, each downloadable and restorable. */
export function VersionsPanel({
  fileId,
  fileName,
  share,
}: {
  fileId: string;
  fileName: string;
  share: Share;
}) {
  const { me } = useSession();
  const versions = useVersions(fileId);
  const download = useDownload();
  const [restoring, setRestoring] = useState<Version | null>(null);
  const [recallVersion, setRecallVersion] = useState<string | null>(null);
  const canRestore = canRestoreVersion(me, share);

  if (versions.isPending) return <Loading label="Loading versions…" />;
  if (versions.isError) return <ErrorCallout error={versions.error} />;
  const items = versions.data.items;
  // A deleted file's newest version is its tombstone; it can only come back as a copy.
  const deleted = items[0]?.kind === "tombstone";

  return (
    <div className="stack">
      <p className="muted">
        This share keeps {share.rewind.depth_days} days of history
        {share.worm.enabled ? ", and WORM keeps every version until its retention ends" : ""}.
      </p>
      {download.status.kind === "preparing" ? (
        <p role="status" className="row">
          <span className="spinner" aria-hidden="true" /> Preparing the download link…
        </p>
      ) : download.status.kind === "started" && isSafeDownloadUrl(download.status.url) ? (
        <Callout tone="ok" role="status">
          Download started. If nothing happens, <a href={download.status.url}>use this link</a>{" "}
          (valid for 10 minutes).
        </Callout>
      ) : download.status.kind === "slow" ? (
        <Callout tone="warn" role="status">
          The link is taking longer than usual. Try again in a minute.
        </Callout>
      ) : download.status.kind === "failed" ? (
        <Callout tone="danger" role="alert">
          {download.status.message}
        </Callout>
      ) : download.status.kind === "needs_recall" ? (
        <Callout tone="warn" title="This version is archived" role="status">
          <p>Recall it first; you can download it once it's back.</p>
          <button
            type="button"
            className="btn btn-sm"
            style={{ marginTop: 8 }}
            onClick={() => {
              if (download.status.kind === "needs_recall")
                setRecallVersion(download.status.download.version_id);
            }}
          >
            Recall this version…
          </button>
        </Callout>
      ) : null}
      <ol className="versions" aria-label={`Versions of ${fileName}`}>
        {items.map((v, i) => (
          <li key={v.version_id} className="version" data-kind={v.kind}>
            <div className="version-main">
              <span className="version-title">
                {i === 0 && v.kind !== "tombstone" ? (
                  <span className="badge badge-accent">Current</span>
                ) : null}
                {KIND_LABEL[v.kind]}
              </span>
              <span className="muted num">{formatDateTime(v.committed_at)}</span>
            </div>
            <div className="row">
              {v.kind !== "tombstone" ? <span className="num">{formatBytes(v.size)}</span> : null}
              {v.tier ? <TierChip tier={v.tier} showNative={false} /> : null}
              {v.legal_hold ? (
                <span className="badge badge-info">
                  <ShieldIcon size={12} /> Legal hold
                </span>
              ) : null}
              {v.retention_until ? (
                <span className="badge badge-outline">
                  Retained until {formatDate(v.retention_until)}
                </span>
              ) : null}
            </div>
            {v.kind !== "tombstone" ? (
              <div className="row">
                <button
                  type="button"
                  className="btn btn-sm"
                  aria-label={`Download the version from ${formatDateTime(v.committed_at)}`}
                  onClick={() => void download.start(fileId, v.version_id)}
                  disabled={download.status.kind === "preparing"}
                >
                  <DownloadIcon /> Download
                </button>
                {canRestore && v.restorable !== false && i > 0 ? (
                  <button
                    type="button"
                    className="btn btn-sm"
                    aria-label={`Restore the version from ${formatDateTime(v.committed_at)}`}
                    onClick={() => setRestoring(v)}
                  >
                    <RewindIcon /> Restore…
                  </button>
                ) : null}
              </div>
            ) : null}
          </li>
        ))}
      </ol>
      {restoring ? (
        <RestoreDialog
          open
          onClose={() => setRestoring(null)}
          fileId={fileId}
          fileName={fileName}
          version={restoring}
          deleted={deleted}
          worm={share.worm.enabled}
        />
      ) : null}
      {recallVersion ? (
        <RecallDialog
          open
          onClose={() => {
            setRecallVersion(null);
            download.reset();
          }}
          fileId={fileId}
          fileName={fileName}
          versionId={recallVersion}
        />
      ) : null}
    </div>
  );
}

const RESTORE_STATE = {
  requested: "Queued",
  waiting_recall: "Waiting for the version to come back from Archive",
  copying: "Copying",
  done: "Done",
  failed: "Failed",
} as const;

/**
 * Decision 34: restore an old version by replacing the current file (a new
 * version, so nothing is lost and WORM allows it), or as a new file next to
 * it. The names shown are worked out here for the preview; the server decides.
 */
export function RestoreDialog({
  open,
  onClose,
  fileId,
  fileName,
  version,
  deleted = false,
  worm = false,
}: {
  open: boolean;
  onClose: () => void;
  fileId: string;
  fileName: string;
  version: Version;
  deleted?: boolean;
  worm?: boolean;
}) {
  const restore = useRestoreVersion();
  const toast = useToast();
  const [target, setTarget] = useState<RestoreTarget>(deleted ? "restored" : "overwrite");
  const [taken, setTaken] = useState<Set<RestoreTarget>>(new Set());
  const [restoreId, setRestoreId] = useState<string | null>(null);
  const progress = useVersionRestore(restoreId);
  const now = useNow(1000, !restoreId);
  const state = progress.data?.state;
  const plainName = restoredName(fileName);
  const stampedName = restoredTimestampedName(fileName, now);
  const nameFor = (t: RestoreTarget) =>
    t === "restored" ? plainName : t === "restored_timestamped" ? stampedName : fileName;
  const nameTaken =
    restore.error instanceof ApiError && restore.error.code === "name_taken" ? restore.error : null;
  const failedTaken = state === "failed" && (progress.data?.failure ?? "").startsWith("name_taken");

  async function submit() {
    try {
      const result = await restore.mutateAsync({ fileId, versionId: version.version_id, target });
      setRestoreId(result.restore_id);
      toast(
        target === "overwrite"
          ? `Restoring ${fileName}.`
          : `Restoring as ${result.name ?? nameFor(target)}.`,
        "info",
      );
    } catch (error) {
      if (error instanceof ApiError && error.code === "name_taken") {
        const next = new Set(taken).add(target);
        setTaken(next);
        const other = (["restored", "restored_timestamped", "overwrite"] as const).find(
          (t) => !next.has(t) && !(t === "overwrite" && deleted),
        );
        if (other) setTarget(other);
      }
    }
  }

  const options: { value: RestoreTarget; title: ReactNode; body: string }[] = [
    {
      value: "overwrite",
      title: "Replace the current file",
      body: deleted
        ? "Not for a deleted file: restore it under a new name instead."
        : `${fileName} gets this version's content. What it holds now is kept as an older version, so nothing is lost${worm ? ". Allowed on this WORM share: it adds a version" : ""}.`,
    },
    {
      value: "restored",
      title: (
        <>
          Restore as <span className="mono">{plainName}</span>
        </>
      ),
      body: `A new file next to ${fileName}; the original is unchanged.`,
    },
    {
      value: "restored_timestamped",
      title: (
        <>
          Restore as <span className="mono">{stampedName}</span>
        </>
      ),
      body: "A new file named with the time of the restore (UTC), so it never clashes with an earlier one.",
    },
  ];

  return (
    <Modal
      open={open}
      onClose={onClose}
      title={`Restore ${fileName}`}
      description={`The version from ${formatDateTime(version.committed_at)}, ${formatBytes(version.size)}.`}
      footer={
        restoreId ? (
          <button type="button" className="btn btn-primary" onClick={onClose}>
            {state === "done" ? "Done" : "Close"}
          </button>
        ) : (
          <>
            <button type="button" className="btn" onClick={onClose}>
              Cancel
            </button>
            <button
              type="button"
              className="btn btn-primary"
              disabled={restore.isPending || taken.has(target)}
              onClick={() => void submit()}
            >
              {restore.isPending ? "Starting…" : "Restore"}
            </button>
          </>
        )
      }
    >
      {restoreId ? (
        <div className="stack" role="status" aria-live="polite">
          {state ? (
            <p>
              <strong>{RESTORE_STATE[state]}</strong>
              {state === "waiting_recall" && progress.data?.recall_id ? (
                <>
                  {" "}
                  · <Link to={`/recalls/${progress.data.recall_id}`}>Follow the recall</Link>
                </>
              ) : null}
            </p>
          ) : (
            <p>Queued…</p>
          )}
          {state === "done" ? (
            <Callout tone="ok">
              {target === "overwrite"
                ? `${fileName} now has this version's content. What it held before is in its versions.`
                : `${progress.data?.name ?? nameFor(target)} is next to ${fileName}.`}
            </Callout>
          ) : null}
          {failedTaken ? (
            <Callout tone="warn">
              {progress.data?.name ?? nameFor(target)} appeared in the folder before the restore
              finished, so nothing was written. Restore again with another option.
            </Callout>
          ) : state === "failed" ? (
            <Callout tone="danger">{progress.data?.failure ?? "The restore failed."}</Callout>
          ) : null}
        </div>
      ) : (
        <>
          <fieldset className="options">
            <legend className="field-label">How?</legend>
            {options.map((o) => (
              <label key={o.value} className="option">
                <input
                  type="radio"
                  name="restore-target"
                  value={o.value}
                  checked={target === o.value}
                  disabled={(o.value === "overwrite" && deleted) || taken.has(o.value)}
                  onChange={() => setTarget(o.value)}
                />
                <span className="option-title">{o.title}</span>
                <span className="option-body">
                  {taken.has(o.value)
                    ? `${nameFor(o.value)} already exists in this folder. `
                    : null}
                  {o.body}
                </span>
              </label>
            ))}
          </fieldset>
          <p className="field-hint">
            Names are a preview: the server sets them when you restore, and may shorten a very long
            one.
          </p>
          {version.tier?.archived ? (
            <Callout tone="warn">
              This version is archived. It is recalled first, and the restore finishes when it's
              back.
            </Callout>
          ) : null}
          {nameTaken ? (
            <Callout tone="warn" title="That name is taken" role="alert">
              {nameTaken.problem?.detail ?? "A file with that name already exists in this folder."}{" "}
              Nothing was changed. Choose one of the other options.
            </Callout>
          ) : restore.isError ? (
            <ErrorCallout error={restore.error} />
          ) : null}
        </>
      )}
    </Modal>
  );
}

/** The current time, ticking while `active`, for the timestamped name preview. */
function useNow(everyMs: number, active: boolean): Date {
  const [now, setNow] = useState(() => new Date());
  useEffect(() => {
    if (!active) return;
    const timer = setInterval(() => setNow(new Date()), everyMs);
    return () => clearInterval(timer);
  }, [everyMs, active]);
  return now;
}
