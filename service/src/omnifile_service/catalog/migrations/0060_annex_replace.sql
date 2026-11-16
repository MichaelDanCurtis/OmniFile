-- Migration 0060: a placement may replace the file at its origin (annex phase A3.4;
-- docs/milestones/results/a34.md; decisions 82 to 85).
--
-- With `overwrite_keep_version`, a file coming back to its own name in its original folder
-- may replace the file there, when that file is exactly the annex's current, verified
-- copy. The edge hides it (the remover's W1), waits the annex's grace period, places the
-- new file (P4) and only then deletes the hidden one (W2). The service answers two
-- questions on the way, `replace_go` before the hide and `replace_delete_go` before the
-- link and again before the delete, and records each step here.
--
-- replace_state: NULL (no replace looked at), `offered` (the ticket carried one),
--   `hidden`, `put_back` (the old file is back under its own name, nothing replaced),
--   `replaced` (the old file deleted), `kept_aside` (put back under a `-replaced-` name
--   because a gate failed after the new file had its name), `gone` (the old file went
--   by a user's hand; nothing deleted by the edge), or `refused`.
-- replace_refused: why a replace wasn't offered or was given up (the placement then
--   takes a `-restored` name, and its outcome says `overwrite_refused_<why>`).
-- replaced_version_id, replaced_crc64: the version the replaced file was, which stays
--   in the file's history: "replaced; the old content is version N".
-- replace_hidden_name, replace_hidden_at: the old file's hidden name and when.
-- replaced_aside_name: the `-replaced-<UTC time>` name the old file was put back under.
-- replace_asked_at: when a replace was first held for its in-Azure read; after
--   `AZURE_READ_WAIT` the placement takes a `-restored` name instead.
-- replace_go_at, replace_place_at, replace_delete_at: when the service last said yes
--   to `replace_go`, and to `replace_delete_go` for the place and the delete phase.
--   A fact is recorded only after its yes (`replace_hidden` after `replace_go`,
--   `replaced` after the delete phase); an offer that ended with no `replace_go` yes
--   hid nothing.
ALTER TABLE catalog.annex_placement
    ADD COLUMN replace_state text CHECK (replace_state ~ '^[a-z][a-z_]{0,31}$'),
    ADD COLUMN replace_refused text CHECK (length(replace_refused) <= 64),
    ADD COLUMN replaced_version_id bigint,
    ADD COLUMN replaced_crc64 text CHECK (replaced_crc64 ~ '^[0-9a-f]{16}$'),
    ADD COLUMN replace_hidden_name text CHECK (length(replace_hidden_name) <= 512),
    ADD COLUMN replace_hidden_at timestamptz,
    ADD COLUMN replaced_aside_name text CHECK (length(replaced_aside_name) <= 255),
    ADD COLUMN replace_asked_at timestamptz,
    ADD COLUMN replace_go_at timestamptz,
    ADD COLUMN replace_place_at timestamptz,
    ADD COLUMN replace_delete_at timestamptz;

-- The scans' question "is a replace open at this path" and "is another placement
-- replacing this file" look here.
CREATE INDEX annex_placement_replacing ON catalog.annex_placement (file_id)
    WHERE replace_state IN ('offered', 'hidden');
