-- Annex phase A3.3, PR #70's review: what a placement's state machine and its
-- copy under another name need.
--
-- before_stuck: the state a stuck placement came from, so that it goes on from
-- its real step when the problem clears.
-- placed_ino: the placed file's inode as the edge journalled it at P5.
-- last_report_at: when the edge last said anything new about it (a repeated
-- `stuck` isn't news), which is how an ArchiveAdmin may abandon it.
-- filer.placement_sid: the registered placement account: the only owner of a
-- staged file that counts as the file's own owner at `go`.
-- filer.placement_account, placement_account_at: the account the filer's own
-- edge says it places files with (SID, kind and name from winbind, or why it
-- can't say), and when. It registers nothing: an ArchiveAdmin does, and only
-- what this says, while it is current.
-- breaker_cleared_at: when an Owner last looked at a pinned file. The loop
-- breaker counts returns since then.
-- purge_refused_at: when a purge last met a legal hold or WORM. The request is
-- cleared, so a refused purge holds nothing up, and a retention asks again later.
-- last_edge_report: where the edge last said a stuck placement was, so that an
-- abandon can say what the file was left like.

ALTER TABLE catalog.annex_placement
    ADD COLUMN before_stuck text,
    ADD COLUMN placed_ino text,
    ADD COLUMN last_report_at timestamptz,
    ADD COLUMN last_edge_report jsonb;

ALTER TABLE catalog.filer
    ADD COLUMN placement_sid text,
    ADD COLUMN placement_account jsonb,
    ADD COLUMN placement_account_at timestamptz;

ALTER TABLE catalog.annex_file
    ADD COLUMN breaker_cleared_at timestamptz,
    ADD COLUMN purge_refused_at timestamptz;
