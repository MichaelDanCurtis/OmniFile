-- Migration 0030: who made a namespace change on the edge (control-api.md
-- section 4.1, `actor`; namespace-sync.md "Who did it"). Ported from line B
-- (PR #10, its migration 0018: d981ee9, 81fa187), on this line's schema.
--
-- The edge sends users' renames, unlinks, owner changes and new folders
-- through POST .../changes (namespace-sync.md). Each op may now carry an
-- `actor`, shaped like a recall's requester (section 4.4) plus
-- `via = unknown`: an NFS caller (FSAL_OMNI's NS_HINT), an SMB session or a
-- local process (the daemon's namespace watcher), or nobody the edge could
-- name (a change its reconcile pass found after the fact, say while the
-- daemon was down). The version the op writes records it, so a tombstone
-- says who deleted the file: the cutover report's `deleted_by`
-- (portal-api.md section 5.18, decision 40), rewind and audit.
--
-- `actor_proof` is decided by the service, never taken from the edge
-- (decision 28, as for recall requesters): `proven` only for Kerberos NFS on
-- a Kerberos-only share, or SMB on a `proven` share; `claimed` otherwise
-- (sec=sys, a local process); `unknown` when the edge could not tell who.
-- `actor_identity_id` is the catalog identity of the actor's SID (added, as
-- for requesters, when the SID passes the new-SID checks), else of its uid.
-- Versions written by anything but an edge op with an actor keep NULLs.

ALTER TABLE catalog.file_version
    ADD COLUMN actor_via         text
        CHECK (actor_via IN ('nfs', 'smb', 'local', 'unknown')),
    ADD COLUMN actor_uid         bigint CHECK (actor_uid >= 0),
    ADD COLUMN actor_sid         catalog.sid,
    ADD COLUMN actor_sec         text
        CHECK (actor_sec IN ('sys', 'krb5', 'krb5i', 'krb5p', 'ntlm', 'kerberos')),
    ADD COLUMN actor_proof       text
        CHECK (actor_proof IN ('claimed', 'proven', 'unknown')),
    ADD COLUMN actor_client_addr inet,
    ADD COLUMN actor_identity_id bigint REFERENCES catalog.identity,
    ADD CONSTRAINT file_version_actor_complete
        CHECK ((actor_via IS NULL) = (actor_proof IS NULL)),
    -- Nobody named: no identity at all.
    ADD CONSTRAINT file_version_actor_unknown
        CHECK (actor_via IS DISTINCT FROM 'unknown'
               OR (actor_proof = 'unknown' AND actor_uid IS NULL AND actor_sid IS NULL
                   AND actor_identity_id IS NULL));
