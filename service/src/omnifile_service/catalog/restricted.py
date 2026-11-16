"""Restricted folders and host-trusted shares (access-control.md §3; threat
model T7).

A folder is restricted when neither Everyone nor Domain Users can read it.
NFS `sec=sys` can't prove who a user is, so a host-trusted share over a
restricted folder needs an ArchiveAdmin's recorded override. The service
checks that when a share is made or changed; an Owner may tighten a folder's
ACL later, so the share's checks (and the list of shares) read the ACLs as
they are now, and a gauge and an alert report every share that needs an
answer. The owner chose a warning over refusing to serve (2026-10-16): a
refusal would let anyone who may tighten one folder take the share offline.
"""

from __future__ import annotations

#: An ACL of the dataset `{ds}` (an SQL expression) that Everyone (S-1-1-0)
#: and Domain Users (RID 513) can't read.
RESTRICTED_ACL_EXISTS = (
    "EXISTS (SELECT 1 FROM catalog.acl a WHERE a.dataset_id = {ds}"
    " AND NOT ('S-1-1-0' = ANY (a.read_sids::text[]))"
    " AND NOT EXISTS (SELECT 1 FROM unnest(a.read_sids::text[]) s"
    "                  WHERE s LIKE '%%-513'))"
)

#: Host-trusted shares over restricted folders with no override recorded.
UNANSWERED_SHARES = (
    "SELECT sh.share_id, sh.name FROM catalog.share sh"
    " WHERE 'sys' = ANY (sh.nfs_sec) AND NOT sh.host_trust_override"
    " AND " + RESTRICTED_ACL_EXISTS.format(ds="sh.dataset_id") + " ORDER BY sh.share_id"
)
