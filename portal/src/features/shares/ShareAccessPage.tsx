import { useState } from "react";
import { Link, useParams } from "react-router";
import { usePutShareAccess, useShare } from "../../api/queries";
import type { GroupRef, Share } from "../../api/types";
import { useSession } from "../../app/session";
import { usePageTitle } from "../../app/usePageTitle";
import { Callout, ErrorCallout, Loading } from "../../components/Feedback";
import { GroupPicker, groupName } from "../../components/GroupPicker";
import { useToast } from "../../components/Toasts";
import { formatDateTime } from "../../lib/format";
import { canEditAccess } from "../../lib/permissions";

type Role = "owners" | "writers" | "readers";

const ROLES: { role: Role; title: string; body: string }[] = [
  {
    role: "owners",
    title: "Owners",
    body: "Read and write; set rewind depth, what people see on archived files, pins and holds, and the Writers and Readers groups.",
  },
  { role: "writers", title: "Writers", body: "Read and write; restore old versions." },
  { role: "readers", title: "Readers", body: "Read, recall ahead of time, rewind and download." },
];

export function ShareAccessPage() {
  const { shareId = "" } = useParams();
  const share = useShare(shareId);
  usePageTitle(share.data ? `${share.data.name} access` : "Share access");
  if (share.isPending) return <Loading />;
  if (share.isError) {
    return (
      <div className="page">
        <ErrorCallout error={share.error} />
      </div>
    );
  }
  return <ShareAccess key={share.data.etag} share={share.data} />;
}

function ShareAccess({ share }: { share: Share }) {
  const { me } = useSession();
  const put = usePutShareAccess(share.share_id);
  const toast = useToast();
  const [groups, setGroups] = useState<Record<Role, GroupRef | null>>({
    owners: share.groups.owners,
    writers: share.groups.writers ?? null,
    readers: share.groups.readers ?? null,
  });
  const [editing, setEditing] = useState<Role | null>(null);
  const changed = (["owners", "writers", "readers"] as Role[]).filter(
    (r) =>
      (groups[r]?.sid ?? null) !==
      ((r === "owners" ? share.groups.owners : share.groups[r])?.sid ?? null),
  );

  return (
    <div className="page">
      <nav aria-label="Breadcrumb">
        <ol className="crumbs">
          <li>
            <Link to="/shares">Shares</Link>
          </li>
          <li>
            <Link to={`/shares/${share.share_id}`}>{share.name}</Link>
          </li>
          <li>
            <span aria-current="page">Access</span>
          </li>
        </ol>
      </nav>
      <div className="page-head">
        <div>
          <h1>Who has access to {share.name}</h1>
          <p className="page-question">Which AD groups can read and change this share?</p>
          <p className="lede">
            Changes reach the gateway as the share's root ACL; folder ACLs set in Windows Explorer
            stay as they are.
          </p>
        </div>
      </div>
      <div className="access-grid">
        {ROLES.map(({ role, title, body }) => {
          const editable = canEditAccess(me, share, role);
          const group = groups[role];
          return (
            <section key={role} className="card" aria-labelledby={`role-${role}`}>
              <div className="card-head">
                <h2 id={`role-${role}`}>{title}</h2>
                {changed.includes(role) ? <span className="badge badge-warn">Changed</span> : null}
              </div>
              <div className="card-body stack">
                <p className="muted">{body}</p>
                <div className="stack" style={{ gap: 2 }}>
                  <strong>{groupName(group)}</strong>
                  {group ? (
                    <>
                      {group.sam_account_name ? (
                        <span className="mono muted">{group.sam_account_name}</span>
                      ) : null}
                      <span className="mono muted">{group.sid}</span>
                    </>
                  ) : null}
                </div>
                {editable ? (
                  editing === role ? (
                    <>
                      <GroupPicker
                        label={`New ${title} group`}
                        onPick={(g) => {
                          setGroups((all) => ({ ...all, [role]: g }));
                          setEditing(null);
                        }}
                      />
                      <button
                        type="button"
                        className="btn btn-sm btn-ghost"
                        onClick={() => setEditing(null)}
                      >
                        Cancel
                      </button>
                    </>
                  ) : (
                    <div className="row">
                      <button type="button" className="btn btn-sm" onClick={() => setEditing(role)}>
                        Change {title.toLowerCase()} group…
                      </button>
                      {role !== "owners" && group ? (
                        <button
                          type="button"
                          className="btn btn-sm btn-ghost"
                          onClick={() => setGroups((all) => ({ ...all, [role]: null }))}
                        >
                          Remove
                        </button>
                      ) : null}
                    </div>
                  )
                ) : (
                  <p className="field-hint">
                    {role === "owners"
                      ? "Only an ArchiveAdmin can change the Owners group."
                      : "Owners or an ArchiveAdmin can change this."}
                  </p>
                )}
              </div>
            </section>
          );
        })}
      </div>
      <div className="save-bar" role="region" aria-label="Save access" style={{ marginTop: 16 }}>
        <strong className="spacer">
          {changed.length ? `${changed.length} group(s) changed` : "No changes"}
        </strong>
        <button
          type="button"
          className="btn"
          disabled={!changed.length}
          onClick={() =>
            setGroups({
              owners: share.groups.owners,
              writers: share.groups.writers ?? null,
              readers: share.groups.readers ?? null,
            })
          }
        >
          Reset
        </button>
        <button
          type="button"
          className="btn btn-primary"
          disabled={!changed.length || put.isPending || !groups.owners}
          onClick={() =>
            put.mutate(
              {
                etag: share.etag,
                owners_group_sid: groups.owners?.sid ?? share.groups.owners.sid,
                writers_group_sid: groups.writers?.sid ?? null,
                readers_group_sid: groups.readers?.sid ?? null,
              },
              { onSuccess: () => toast(`Access saved for ${share.name}.`) },
            )
          }
        >
          {put.isPending ? "Saving…" : "Save access"}
        </button>
      </div>
      {put.isError ? <ErrorCallout error={put.error} title="Not saved" /> : null}
      <div style={{ marginTop: 16 }}>
        <Callout>
          {share.trust_level === "host_trusted"
            ? "This share is host-trusted (NFS sec=sys): the uid is whatever the client says, so these groups are only as strong as the client hosts."
            : "Users on this share are proven by Kerberos or SMB sign-in."}{" "}
          ACLs were last scanned {formatDateTime(share.last_full_acl_scan_at)}.
        </Callout>
      </div>
    </div>
  );
}
