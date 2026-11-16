-- Recall requesters may be `local`: a process on the edge itself (an admin
-- shell, a backup agent) that read a stub directly through the data volume.
-- The smb-guard catches those reads like SMB ones and the daemon reports them
-- with via = local (control API §4.4, M2 integration).
ALTER TABLE catalog.recall_requester
    DROP CONSTRAINT recall_requester_via_check,
    ADD CONSTRAINT recall_requester_via_check
        CHECK (via IN ('nfs', 'smb', 'local', 'portal', 'teams', 'api'));
