import { useId, useState } from "react";
import { useLocation, useSearchParams } from "react-router";
import { signIn } from "../../api/client";
import { useWorm } from "../../api/queries";
import type { Share } from "../../api/types";
import { authIsRecent, useSession } from "../../app/session";
import { Callout, ErrorCallout } from "../../components/Feedback";
import { LockIcon } from "../../components/icons";
import { Modal } from "../../components/Modal";
import { WORM_QUIET_S } from "./QuietCard";
import { useToast } from "../../components/Toasts";
import { formatDateTime, formatRelative, pluralize } from "../../lib/format";

/** WORM is one-way once locked (decision 31): typed share name, re-auth, lock deadline shown. */
export function WormCard({ share }: { share: Share }) {
  const { session } = useSession();
  const worm = useWorm(share.share_id);
  const toast = useToast();
  // Back from a re-auth sign-in (?worm=enable|cancel): reopen that step, but
  // only when it still applies, and drop the param so a reload doesn't reopen it.
  const [params, setParams] = useSearchParams();
  const initial = params.get("worm");
  const cancellable = share.worm.enabled && share.worm.mode !== "locked";
  const [dialog, setDialog] = useState<"enable" | "cancel" | null>(
    initial === "enable" && !share.worm.enabled
      ? "enable"
      : initial === "cancel" && cancellable
        ? "cancel"
        : null,
  );
  const [extendTo, setExtendTo] = useState(String(share.worm.retention_days ?? 0));
  const extendId = useId();
  const w = share.worm;

  return (
    <section className="card danger-zone" aria-labelledby="worm-title">
      <div className="card-head">
        <LockIcon />
        <h2 id="worm-title">WORM (write once, read many)</h2>
        <span className="spacer" />
        {w.enabled ? (
          <span className="badge badge-info">
            {w.mode === "locked" ? "Locked" : "In its test window"}
          </span>
        ) : (
          <span className="badge">Off</span>
        )}
      </div>
      <div className="card-body stack">
        {!w.enabled ? (
          <>
            <p>
              With WORM on, every committed version is kept for the retention period. Overwrites and
              deletes only add a new version or a tombstone; nothing is destroyed early.
            </p>
            <Callout tone="warn" title="This is one-way">
              After a short test window the policy locks. From then on nobody, including Microsoft
              support, can turn it off or shorten retention. Mutable data needs a new share.
            </Callout>
            <div className="row">
              <button type="button" className="btn btn-danger" onClick={() => setDialog("enable")}>
                Turn on WORM…
              </button>
            </div>
          </>
        ) : (
          <>
            <dl className="kv">
              <dt>Retention</dt>
              <dd>{pluralize(w.retention_days ?? 0, "day")} from each version's commit</dd>
              <dt>Turned on</dt>
              <dd>{formatDateTime(w.enabled_at)}</dd>
              {w.mode === "locked" ? (
                <>
                  <dt>Locked</dt>
                  <dd>{formatDateTime(w.locked_at)}</dd>
                </>
              ) : (
                <>
                  <dt>Locks</dt>
                  <dd>
                    <strong>{formatDateTime(w.lock_deadline)}</strong> (
                    {formatRelative(w.lock_deadline)})
                  </dd>
                </>
              )}
            </dl>
            {w.mode !== "locked" ? (
              <Callout tone="warn" title="Test window">
                Until {formatDateTime(w.lock_deadline)} you can still turn WORM off. After that it
                is permanent.
              </Callout>
            ) : null}
            <form
              className="sentence"
              onSubmit={(e) => {
                e.preventDefault();
                worm.extend.mutate(
                  { etag: share.etag, retention_days: Number(extendTo) },
                  { onSuccess: () => toast("Retention updated.") },
                );
              }}
            >
              <label htmlFor={extendId}>Keep versions for</label>
              <input
                id={extendId}
                className="inline-num"
                type="number"
                min={w.mode === "locked" ? (w.retention_days ?? 1) : 1}
                max={36500}
                value={extendTo}
                onChange={(e) => setExtendTo(e.target.value)}
              />
              <span>days</span>
              <button
                type="submit"
                className="btn btn-sm"
                disabled={worm.extend.isPending || Number(extendTo) === (w.retention_days ?? 0)}
              >
                {w.mode === "locked" ? "Extend" : "Change"}
              </button>
              {w.mode === "locked" ? (
                <span className="field-hint">Locked: it can only go up.</span>
              ) : null}
            </form>
            {worm.extend.isError ? <ErrorCallout error={worm.extend.error} /> : null}
            {w.mode !== "locked" ? (
              <div className="row">
                <button type="button" className="btn" onClick={() => setDialog("cancel")}>
                  Turn WORM off (test window only)…
                </button>
              </div>
            ) : null}
          </>
        )}
      </div>
      {dialog ? (
        <WormDialog
          share={share}
          action={dialog}
          recentAuth={authIsRecent(session)}
          onClose={() => {
            setDialog(null);
            if (params.has("worm")) {
              const next = new URLSearchParams(params);
              next.delete("worm");
              setParams(next, { replace: true });
            }
          }}
          worm={worm}
        />
      ) : null}
    </section>
  );
}

function WormDialog({
  share,
  action,
  recentAuth,
  onClose,
  worm,
}: {
  share: Share;
  action: "enable" | "cancel";
  recentAuth: boolean;
  onClose: () => void;
  worm: ReturnType<typeof useWorm>;
}) {
  const toast = useToast();
  const location = useLocation();
  const [retention, setRetention] = useState("2555");
  const [windowHours, setWindowHours] = useState("24");
  const [typed, setTyped] = useState("");
  // Decision 67: a share already set below the WORM default is asked about here,
  // as lowering it on a WORM share is (#80 re-check).
  const shortQuiet = share.quiet_s != null && share.quiet_s < WORM_QUIET_S;
  const [quietOk, setQuietOk] = useState(false);
  const nameId = useId();
  const retId = useId();
  const winId = useId();
  const [openedAt] = useState(() => Date.now());
  const matches = typed === share.name;
  const hours = Math.min(24, Math.max(1, Number(windowHours) || 24));
  const lockAt = new Date(openedAt + hours * 3600_000).toISOString();
  const mutation = action === "enable" ? worm.enable : worm.cancel;

  function submit() {
    if (action === "enable") {
      worm.enable.mutate(
        {
          etag: share.etag,
          retention_days: Number(retention),
          confirm_name: typed,
          window_hours: hours,
          quiet_s_confirmed: shortQuiet ? true : undefined,
        },
        {
          onSuccess: (s) => {
            toast(
              `WORM is on for ${share.name}. It locks ${formatDateTime(s.worm.lock_deadline)}.`,
            );
            onClose();
          },
        },
      );
    } else {
      worm.cancel.mutate(
        { etag: share.etag, confirm_name: typed },
        {
          onSuccess: () => {
            toast(`WORM is off for ${share.name}.`);
            onClose();
          },
        },
      );
    }
  }

  return (
    <Modal
      open
      onClose={onClose}
      title={
        action === "enable" ? `Turn on WORM for ${share.name}` : `Turn WORM off for ${share.name}`
      }
      footer={
        <>
          <button type="button" className="btn" onClick={onClose}>
            Cancel
          </button>
          {recentAuth ? (
            <button
              type="button"
              className="btn btn-danger"
              disabled={
                !matches ||
                mutation.isPending ||
                (action === "enable" && !(Number(retention) >= 1)) ||
                (action === "enable" && shortQuiet && !quietOk)
              }
              onClick={submit}
            >
              {mutation.isPending
                ? "Working…"
                : action === "enable"
                  ? "Turn on WORM"
                  : "Turn WORM off"}
            </button>
          ) : (
            <button
              type="button"
              className="btn btn-primary"
              onClick={() =>
                signIn({ reauth: true, returnTo: `${location.pathname}?worm=${action}` })
              }
            >
              Sign in again to continue
            </button>
          )}
        </>
      }
    >
      {!recentAuth ? (
        <Callout tone="warn" title="Sign in again first">
          WORM changes need a sign-in within the last 10 minutes. You'll come straight back to this
          step.
        </Callout>
      ) : null}
      {action === "enable" ? (
        <>
          <div className="grid-2">
            <div className="field">
              <label className="field-label" htmlFor={retId}>
                Retention (days)
              </label>
              <input
                id={retId}
                type="number"
                min={1}
                max={36500}
                value={retention}
                onChange={(e) => setRetention(e.target.value)}
              />
              <span className="field-hint">From each version's commit. 2,555 days is 7 years.</span>
            </div>
            <div className="field">
              <label className="field-label" htmlFor={winId}>
                Test window (hours)
              </label>
              <input
                id={winId}
                type="number"
                min={1}
                max={24}
                value={windowHours}
                onChange={(e) => setWindowHours(e.target.value)}
              />
              <span className="field-hint">1 to 24 hours to check it before it locks.</span>
            </div>
          </div>
          <Callout tone="danger" title={`It locks around ${formatDateTime(lockAt)}`}>
            After that, WORM can't be turned off and retention can't be shortened, for {share.name}{" "}
            or any version in it. Smart tier can't be used on this share.
          </Callout>
          {shortQuiet ? (
            <Callout
              tone="warn"
              title={`Files here count as finished after ${share.quiet_s} seconds`}
            >
              That is shorter than the WORM default of {WORM_QUIET_S} seconds. If a copy pauses for
              longer than {(share.quiet_s ?? 0) * 2} seconds, OmniFile keeps the half-written file
              as a version, and once this share is WORM that version can't be deleted until its
              retention ends.
              <label className="check-row">
                <input
                  type="checkbox"
                  checked={quietOk}
                  onChange={(e) => setQuietOk(e.target.checked)}
                />
                <span>Keep {share.quiet_s} seconds anyway (recorded in the audit log)</span>
              </label>
            </Callout>
          ) : null}
        </>
      ) : (
        <Callout tone="warn">
          WORM is still in its test window, so it can be turned off. Versions already written keep
          no retention.
        </Callout>
      )}
      <div className="field">
        <label className="field-label" htmlFor={nameId}>
          Type the share name, <span className="mono">{share.name}</span>, to confirm
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
      {mutation.isError ? <ErrorCallout error={mutation.error} /> : null}
    </Modal>
  );
}
