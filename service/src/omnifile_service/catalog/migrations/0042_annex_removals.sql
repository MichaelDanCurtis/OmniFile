-- Migration 0042: Annex phase A2.2, removals from the NAS (docs/milestones/results/
-- wp14-a2.md, "What writes to the filer"; decisions 51 and 59).
--
-- The edge never decides a removal alone. For every file it asks the service
-- for a ticket, and before each write (the hide, then the delete) it asks
-- again; the service answers from the gates below, in one place, audited:
--
-- - the annex's removals are live: its running rule set removes, its dry run
--   has ended, an ArchiveAdmin confirmed the dry run for that very version
--   (decision 59), and neither the annex nor Settings has paused removals;
-- - the file is the version that was copied, with its NAS attributes as
--   copied, and that copy was read back in Azure within the last 24 hours
--   (`location.azure_read_at`; otherwise a read is queued and the answer is
--   "not yet");
-- - the rate and window allow it.
--
-- - `catalog.location.azure_read_at`: the last in-Azure read of the whole
--   blob (worker/verify.py), and when one was last asked for.
-- - `catalog.annex`: the confirmation (version, who, when).
-- - `catalog.annex_file.removed_at`.
-- - `catalog.annex_removal`: one row per ticket and its steps.
-- - `catalog.annex_ruleset.decided_as_archive_admin`: whether the person who
--   approved the version held ArchiveAdmin then. Removals are live only for
--   a version an ArchiveAdmin approved (the service never lets anyone else
--   approve a removing set; this records it where the ticket gate reads it).

ALTER TABLE catalog.annex_ruleset
    ADD COLUMN decided_as_archive_admin boolean NOT NULL DEFAULT false;

ALTER TABLE catalog.location
    ADD COLUMN azure_read_at           timestamptz,
    ADD COLUMN azure_read_requested_at timestamptz;

ALTER TABLE catalog.annex
    ADD COLUMN removals_confirmed_version integer,
    ADD COLUMN removals_confirmed_by      bigint REFERENCES catalog.identity,
    ADD COLUMN removals_confirmed_at      timestamptz;

ALTER TABLE catalog.annex_file
    ADD COLUMN removed_at timestamptz;

CREATE TABLE catalog.annex_removal (
    removal_id      bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    annex_id        bigint NOT NULL REFERENCES catalog.annex,
    file_id         bigint NOT NULL REFERENCES catalog.file,
    version_id      bigint NOT NULL,
    path            text COLLATE "C" NOT NULL,
    -- What the file must still be on the NAS (as copied).
    nas_file_id     text NOT NULL,
    size            bigint NOT NULL,
    mtime_ns        bigint NOT NULL,
    ctime_ns        bigint,
    rule            text NOT NULL,
    ruleset_version integer NOT NULL,
    state           text NOT NULL DEFAULT 'ticketed'
                    -- stuck: the edge couldn't put a hidden file back (its name
                    -- was taken, or the rename failed): it stays hidden, raised.
                    CHECK (state IN ('ticketed', 'hidden', 'deleting', 'deleted', 'done',
                                     'cancelled', 'stuck')),
    issued_at       timestamptz NOT NULL DEFAULT now(),
    -- The hide must be asked for before this; after it, a new ticket.
    expires_at      timestamptz NOT NULL,
    hidden_name     text,
    hidden_at       timestamptz,
    deleted_at      timestamptz,
    done_at         timestamptz,
    pointer_written boolean,
    cancel_reason   text CHECK (length(cancel_reason) <= 200),
    updated_at      timestamptz NOT NULL DEFAULT now()
);
-- One removal in flight per file.
CREATE UNIQUE INDEX annex_removal_open ON catalog.annex_removal (file_id)
    WHERE state IN ('ticketed', 'hidden', 'deleting', 'stuck');
CREATE INDEX annex_removal_rate ON catalog.annex_removal (annex_id, issued_at);
