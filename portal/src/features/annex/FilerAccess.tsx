import { useState } from "react";
import { ErrorCallout } from "../../components/Feedback";
import { GroupPicker, groupName } from "../../components/GroupPicker";
import type { GroupRef } from "../../api/types";
import { formatDateTime } from "../../lib/format";
import { useAcknowledgeAclsHidden, useAnswerFilerAccess } from "./api";
import type { Filer } from "./api";

/**
 * Decision 65: whether the filer's SMB share or NFS export lets in fewer people than the
 * files' permissions do. Neither can be read through a mount, so an ArchiveAdmin answers;
 * until then the annex's files are hidden from everyone.
 */
export function FilerAccess({ filer, canAnswer }: { filer: Filer; canAnswer: boolean }) {
  const access = filer.access;
  const [editing, setEditing] = useState(false);
  const [limited, setLimited] = useState<boolean>(access?.limited ?? false);
  const [groups, setGroups] = useState<GroupRef[]>([]);
  const answer = useAnswerFilerAccess();
  const answered = access?.answered ?? false;
  const summary = !answered ? (
    <span className="badge badge-warn">Not answered: its files are hidden from everyone</span>
  ) : access?.limited ? (
    <span>Only these AD groups: {access.groups.join(", ")}</span>
  ) : (
    <span>No: the files&apos; own permissions decide</span>
  );
  const submit = () =>
    answer.mutate(
      {
        filerId: filer.filer_id,
        access: limited ? { limited: true, groups: groups.map((g) => g.sid) } : { limited: false },
      },
      { onSuccess: () => setEditing(false) },
    );
  return (
    <div className="filer-access">
      {summary}
      {answered && access?.confirmed_at ? (
        <div className="muted micro">
          Confirmed by {access.confirmed_by} on {formatDateTime(access.confirmed_at)}
        </div>
      ) : null}
      {canAnswer && !editing ? (
        <button type="button" className="btn btn-sm" onClick={() => setEditing(true)}>
          {answered ? "Confirm again" : "Answer"}
        </button>
      ) : null}
      {canAnswer && editing ? (
        <fieldset className="stack">
          <legend>Does the share or export let in fewer people than the files do?</legend>
          <label>
            <input type="radio" checked={!limited} onChange={() => setLimited(false)} /> No, the
            files&apos; own permissions decide
          </label>
          <label>
            <input type="radio" checked={limited} onChange={() => setLimited(true)} /> Yes, only
            these AD groups
          </label>
          {limited ? (
            <>
              <p className="muted micro">
                If the export is limited by host or subnet, name the groups whose people use those
                hosts. Everyone else is kept out of these files.
              </p>
              <GroupPicker
                label="Add a group the share lets in"
                onPick={(g) =>
                  setGroups((gs) => (gs.some((x) => x.sid === g.sid) ? gs : [...gs, g]))
                }
              />
              <ul aria-label="Groups the share lets in">
                {groups.map((g) => (
                  <li key={g.sid}>{groupName(g)}</li>
                ))}
              </ul>
            </>
          ) : null}
          <button
            type="button"
            className="btn btn-sm btn-primary"
            disabled={answer.isPending || (limited && groups.length === 0)}
            onClick={submit}
          >
            Confirm
          </button>
          {answer.isError ? (
            <ErrorCallout error={answer.error} title="The answer wasn't saved" />
          ) : null}
        </fieldset>
      ) : null}
    </div>
  );
}

/**
 * Decision 64: the filer's NAS shows no ACLs through its mount, so access there rests on
 * mode bits alone. Group-readable placements and share copies of its files are refused
 * until an ArchiveAdmin acknowledges it.
 */
export function FilerAcls({ filer, canAnswer }: { filer: Filer; canAnswer: boolean }) {
  const acls = filer.acls;
  const ack = useAcknowledgeAclsHidden();
  if (!acls?.hidden_since) return <span>Shown</span>;
  return (
    <div className="filer-acls">
      <span className="badge badge-warn">
        {acls.blocks_grants ? "Not shown: group-readable copies wait" : "Not shown, acknowledged"}
      </span>
      <div className="muted micro">
        Since {formatDateTime(acls.hidden_since)}: who may read a file there rests on its mode bits
        alone.
        {acls.acknowledged_at
          ? ` Acknowledged by ${acls.acknowledged_by ?? ""} on ${formatDateTime(acls.acknowledged_at)}.`
          : " Acknowledge it, or move the annex to NFSv4 or SMB."}
      </div>
      {canAnswer && acls.blocks_grants ? (
        <button
          type="button"
          className="btn btn-sm"
          disabled={ack.isPending}
          onClick={() => ack.mutate({ filerId: filer.filer_id })}
        >
          Acknowledge
        </button>
      ) : null}
      {ack.isError ? (
        <ErrorCallout error={ack.error} title="The acknowledgement wasn't saved" />
      ) : null}
    </div>
  );
}
