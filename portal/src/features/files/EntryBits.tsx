import { Link } from "react-router";
import type { Entry } from "../../api/types";
import { PinIcon, ShieldIcon, ClockIcon, RecallIcon } from "../../components/icons";
import {
  NEXT_ACTION_LABEL,
  RECALL_STATE_LABEL,
  stateLabel,
  tierLabel,
  formatDate,
  formatRelative,
} from "../../lib/format";

export function StateLabel({ entry }: { entry: Pick<Entry, "state" | "kind"> }) {
  if (entry.kind === "dir") return <span className="muted">Folder</span>;
  return (
    <span className="state" data-state={entry.state}>
      {stateLabel(entry.state)}
    </span>
  );
}

/** What happens to the file next, in a few words. */
export function NextStep({ entry }: { entry: Entry }) {
  if (entry.kind === "dir") return <span className="muted">—</span>;
  if (entry.tier.archived) {
    // When the blob really went: a file with the same bytes can archive it before this file's date.
    const since = entry.tier.tier_changed_at;
    return <span className="muted">Archived{since ? ` ${formatDate(since)}` : ""}</span>;
  }
  if (entry.move_hold) {
    return (
      <span title="Another file shares this file's storage, and that storage changed tier in the last day.">
        Move to {tierLabel(entry.move_hold.to)} waiting{" "}
        <span className="muted">until {formatDate(entry.move_hold.until)}</span>
      </span>
    );
  }
  if (entry.next_action && entry.next_action_at) {
    return (
      <span>
        {NEXT_ACTION_LABEL[entry.next_action]}{" "}
        <span className="muted">{formatRelative(entry.next_action_at)}</span>
      </span>
    );
  }
  if (entry.archive_at) {
    return (
      <span>
        Archives <span className="muted">{formatDate(entry.archive_at)}</span>
      </span>
    );
  }
  return <span className="muted">—</span>;
}

export function EntryTags({ entry }: { entry: Entry }) {
  const tags = [];
  if (entry.active_recall) {
    tags.push(
      <Link
        key="recall"
        className="badge badge-accent"
        to={`/recalls/${entry.active_recall.recall_id}`}
        aria-label={`Recall in progress: ${RECALL_STATE_LABEL[entry.active_recall.state]}. Open the recall.`}
      >
        <RecallIcon size={12} /> {RECALL_STATE_LABEL[entry.active_recall.state]}
      </Link>,
    );
  }
  if (entry.read_back_failed_at) {
    tags.push(
      <span
        key="read-back"
        className="badge badge-warn"
        title={`The cloud copy could not be checked on ${formatDate(entry.read_back_failed_at)}. The file stays on the file server. An administrator can try again.`}
      >
        Not checked
      </span>,
    );
  }
  if (entry.pin) {
    tags.push(
      <span
        key="pin"
        className="badge"
        title={entry.pin.inherited_from ? "Pinned on a parent folder" : "Pinned on this item"}
      >
        <PinIcon size={12} /> {tierLabel(entry.pin.tier)}
        {entry.pin.until ? ` until ${formatDate(entry.pin.until)}` : ""}
        {entry.pin.inherited_from ? " (folder)" : ""}
      </span>,
    );
  }
  if (entry.holds.keep_online) {
    tags.push(
      <span key="keep" className="badge badge-ok">
        <ClockIcon size={12} /> Kept online
        {entry.holds.keep_online_until ? ` until ${formatDate(entry.holds.keep_online_until)}` : ""}
      </span>,
    );
  }
  if (entry.holds.legal) {
    tags.push(
      <span key="legal" className="badge badge-info">
        <ShieldIcon size={12} /> Legal hold
      </span>,
    );
  }
  if (tags.length === 0) return null;
  return <span className="tags">{tags}</span>;
}

export function needsRecall(entry: Pick<Entry, "kind" | "tier">): boolean {
  return entry.kind === "file" && entry.tier.archived && entry.tier.restore_state !== "completed";
}
