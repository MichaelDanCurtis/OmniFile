import { useId } from "react";
import type { Share } from "../../api/types";
import { Callout } from "../../components/Feedback";
import { Modal } from "../../components/Modal";

/** The bounds `PATCH /v1/shares/{id}/settings` holds (decision 67). */
export const QUIET_MIN_S = 10;
export const QUIET_MAX_S = 600;
/** The WORM default: below it, saving asks first (#80 review). */
export const WORM_QUIET_S = 60;

export function quietOutOfRange(value: string): boolean {
  const n = Number(value);
  return value.trim() === "" || !Number.isInteger(n) || n < QUIET_MIN_S || n > QUIET_MAX_S;
}

const seconds = (n: number) => `${n} seconds`;

/**
 * How long a file must sit untouched before it counts as finished.
 *
 * The edge can't tell which protocol wrote a file, so the period applies to every
 * write on the share (edge `quiet.rs`). What differs is what it protects: NFSv3
 * relies on it, NFSv4 is held by its open state, and SMB has no such check while a
 * client keeps the file open (`commit.rs`, `still_being_written`).
 */
export function QuietCard({
  share,
  editable,
  own,
  value,
  onOwn,
  onValue,
}: {
  share: Share;
  editable: boolean;
  own: boolean;
  value: string;
  onOwn: (own: boolean) => void;
  onValue: (value: string) => void;
}) {
  const inputId = useId();
  const worm = share.worm.enabled;
  const bad = own && quietOutOfRange(value);
  const current = share.quiet_s ?? share.quiet_default_s;
  const body = (
    <div className="stack">
      <fieldset className="options">
        <legend className="field-label">
          How long a file must sit untouched before OmniFile treats it as finished
        </legend>
        <label className="option">
          <input
            type="radio"
            name="quiet"
            checked={!own}
            disabled={!editable}
            onChange={() => onOwn(false)}
          />
          <span className="option-title">The default: {seconds(share.quiet_default_s)}</span>
          <span className="option-body">
            {worm
              ? "Longer on a WORM share, where a partial version can't be removed until retention ends."
              : "What every share that isn't WORM uses."}
          </span>
        </label>
        <label className="option">
          <input
            type="radio"
            name="quiet"
            checked={own}
            disabled={!editable}
            onChange={() => onOwn(true)}
          />
          <span className="option-title">Choose a time</span>
          <span className="option-body">
            {QUIET_MIN_S} to {QUIET_MAX_S} seconds.
          </span>
        </label>
      </fieldset>
      {own ? (
        <div className="sentence">
          <label htmlFor={inputId}>Wait</label>
          <input
            id={inputId}
            className="inline-num"
            type="number"
            min={QUIET_MIN_S}
            max={QUIET_MAX_S}
            step={1}
            disabled={!editable}
            value={value}
            aria-invalid={bad}
            onChange={(e) => onValue(e.target.value)}
          />
          <span>seconds</span>
        </div>
      ) : null}
      <p className="field-hint">
        Longer means fewer partial versions, but uploads start later: about twice this long after
        the last write.
      </p>
      {share.protocols.includes("nfs3") ? (
        <p className="field-hint">
          NFSv3 has no open or close, so this is how OmniFile decides a file is finished. If a copy
          pauses for longer than twice this time, the file so far is kept as a version, and the
          finished file becomes the next one.
          {worm ? " On this WORM share that partial version stays until retention ends." : ""}
        </p>
      ) : null}
      {share.protocols.includes("nfs4") ? (
        <p className="field-hint">
          NFSv4: a file a client holds open is never treated as finished, whatever this says. After
          the client closes it, the file still waits this long.
        </p>
      ) : null}
      {share.protocols.includes("smb") ? (
        <p className="field-hint">
          SMB: this counts from the last write. A client that pauses with the file still open is
          treated as NFSv3 is, so a long pause can keep a partial version.
        </p>
      ) : null}
      {!editable ? (
        <p className="field-hint">
          Only this share's Owners or an ArchiveAdmin can change how long files wait.
        </p>
      ) : null}
      {!share.quiet_supported ? (
        <Callout tone="warn" title="Not yet supported on its edge">
          {share.edge_name ?? "This share's edge"} hasn't reported that it sets a quiet period for
          each share. It may be running an older version, which uses its own period for every share
          whatever is set here. The default shown is the usual one, not that edge's.
        </Callout>
      ) : null}
    </div>
  );
  return (
    <section className="card" aria-labelledby="quiet-title">
      <div className="card-head">
        <h2 id="quiet-title">When a file counts as finished</h2>
      </div>
      <div className="card-body stack">
        {share.protocols.includes("nfs3") ? (
          body
        ) : (
          <details className="more">
            <summary>
              Files wait {seconds(current)} after their last write. This matters most on NFSv3,
              which this share doesn't serve.
            </summary>
            <div className="more-body">{body}</div>
          </details>
        )}
      </div>
    </section>
  );
}

/** Asked before a WORM share is set below its default (#80 review). */
export function QuietShortConfirm({
  open,
  share,
  seconds,
  onCancel,
  onConfirm,
}: {
  open: boolean;
  share: Share;
  seconds: number;
  onCancel: () => void;
  onConfirm: () => void;
}) {
  return (
    <Modal
      open={open}
      onClose={onCancel}
      title={`Wait only ${seconds} seconds on ${share.name}?`}
      footer={
        <>
          <button type="button" className="btn" onClick={onCancel}>
            Keep it as it was
          </button>
          <button type="button" className="btn btn-danger" onClick={onConfirm}>
            Save {seconds} seconds
          </button>
        </>
      }
    >
      <p>
        This share is WORM. If a copy into it pauses for longer than {seconds * 2} seconds, OmniFile
        keeps the half-written file as a version, and on a WORM share that version can't be deleted
        until its retention ends. The finished file becomes the next version, so nothing is lost,
        but the half-written one stays.
      </p>
      <p className="field-hint">
        The WORM default is {WORM_QUIET_S} seconds. Your name and this choice go in the audit log.
      </p>
    </Modal>
  );
}
