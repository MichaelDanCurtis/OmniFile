-- Migration 0054 for now (after decision 65's 0053): it moves to the next free number
-- at merge.
--
-- Decision 64: a filer that doesn't show its ACLs is flagged, and new grants are
-- blocked there. A server that says it has no ACL support answers as if every file had
-- only its mode; a hidden mask then makes the mode's group bits look like the owning
-- group's, which the NAS may refuse (annex.md section 5).
--
-- - catalog.filer.acls_hidden_since: from the first mount report (the edge checks at
--   every mount, `acls_shown`) that said the server shows no ACLs, until one says it
--   does. The portal flags the filer, and the gauge `omnifile.annex.filers_acls_hidden`
--   keeps an alert up until an ArchiveAdmin acknowledges it.
-- - acls_hidden_ack_by, acls_hidden_ack_at: the acknowledgement that access there rests
--   on mode bits alone (audited, `filer.acls_hidden`). A report that the ACLs show
--   clears the flag and the acknowledgement with it.
-- - Until it is acknowledged, group-readable placements and group-readable share copies
--   of the filer's files are refused (`acls_hidden_filer`); portal names and recalls go
--   on.

ALTER TABLE catalog.filer
    ADD COLUMN acls_hidden_since timestamptz,
    ADD COLUMN acls_hidden_ack_by text,
    ADD COLUMN acls_hidden_ack_at timestamptz,
    ADD CONSTRAINT filer_acls_hidden_ack CHECK (
        (acls_hidden_ack_at IS NULL) = (acls_hidden_ack_by IS NULL)
        AND (acls_hidden_ack_at IS NULL OR acls_hidden_since IS NOT NULL));
