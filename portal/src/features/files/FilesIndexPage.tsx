import { Link, Navigate, useParams } from "react-router";
import { useShare, useShares } from "../../api/queries";
import type { Share } from "../../api/types";
import { usePageTitle } from "../../app/usePageTitle";
import { Empty, ErrorCallout, Loading } from "../../components/Feedback";
import { LockIcon, ShareIcon } from "../../components/icons";

export function shareRoleLabel(share: Share): string {
  const a = share.my_access;
  return a.owner ? "Owner" : a.writer ? "Writer" : a.reader ? "Reader" : "No share role";
}

export function FilesIndexPage() {
  usePageTitle("Files");
  const shares = useShares();
  return (
    <div className="page">
      <div className="page-head">
        <div>
          <h1>Files</h1>
          <p className="page-question">Which shares can you read, and where are their files?</p>
          <p className="lede">
            Open a share to bring archived files back ahead of time, or to go back to earlier
            versions.
          </p>
        </div>
      </div>
      {shares.isPending ? (
        <Loading />
      ) : shares.isError ? (
        <ErrorCallout error={shares.error} />
      ) : shares.data.items.length === 0 ? (
        <Empty title="No shares yet">
          <p>Your AD groups don't give you read access to any share.</p>
        </Empty>
      ) : (
        <ul className="share-cards" aria-label="Shares">
          {shares.data.items.map((share) => (
            <li key={share.share_id}>
              {share.my_access.can_read_root ? (
                <Link
                  className="card share-card"
                  to={`/files/${share.share_id}/${share.root_dir_id}`}
                >
                  <ShareCardBody share={share} />
                </Link>
              ) : (
                <div className="card share-card" aria-disabled="true">
                  <ShareCardBody share={share} />
                  <p className="muted">
                    <LockIcon size={13} /> You manage this share but can't read its files.
                  </p>
                </div>
              )}
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

function ShareCardBody({ share }: { share: Share }) {
  return (
    <>
      <span className="row">
        <ShareIcon />
        <strong className="share-card-name">{share.name}</strong>
        {share.worm.enabled ? <span className="badge badge-info">WORM</span> : null}
      </span>
      <span className="muted">{share.dataset_name}</span>
      <span className="row">
        {share.protocols.map((p) => (
          <span key={p} className="badge badge-outline mono">
            {p === "nfs4" ? "NFSv4" : p === "nfs3" ? "NFSv3" : "SMB"}
          </span>
        ))}
        <span className="badge">{shareRoleLabel(share)}</span>
      </span>
    </>
  );
}

export function ShareRootRedirect() {
  const { shareId } = useParams();
  const share = useShare(shareId);
  if (share.isPending) return <Loading />;
  if (share.isError)
    return (
      <div className="page">
        <ErrorCallout error={share.error} />
      </div>
    );
  return <Navigate to={`/files/${share.data.share_id}/${share.data.root_dir_id}`} replace />;
}
