-- Migration 0012: portal follow-ups (M4 review; decision 34).
--
-- 1. version_restore.target: `overwrite` (the old bytes become a new current
--    version), `restored` (a new file `<stem>-restored<ext>`) and
--    `restored_timestamped` (`<stem>-restored-<YYYY-MM-DD-HHMMSS>Z<ext>`).
--    They replace `current` and `copy`; rows already there are mapped.
-- 2. version_restore.copy_*: the blob the worker copied, recorded before the
--    catalog rows are written, so a retried restore (throttled, say) resumes
--    from `copying` without copying again.
-- 3. policy_proposal.share_overrides: a proposal's "what users see" answers
--    (on_open, wait_threshold_minutes per share), applied to the shares only
--    when an ArchiveAdmin approves the proposal (anti-pattern 7).

ALTER TABLE catalog.version_restore
    DROP CONSTRAINT version_restore_target_check,
    DROP CONSTRAINT version_restore_copy_name;
UPDATE catalog.version_restore
   SET target = CASE target WHEN 'current' THEN 'overwrite' ELSE 'restored' END;
ALTER TABLE catalog.version_restore
    ADD CONSTRAINT version_restore_target_check
        CHECK (target IN ('overwrite', 'restored', 'restored_timestamped')),
    ADD CONSTRAINT version_restore_new_name
        CHECK ((target = 'overwrite') = (name IS NULL)),
    ADD COLUMN copy_container         text COLLATE "C",
    ADD COLUMN copy_key               text COLLATE "C",
    ADD COLUMN copy_backend_version_id text,
    ADD CONSTRAINT version_restore_copy_ref
        CHECK ((copy_container IS NULL) = (copy_key IS NULL));

ALTER TABLE catalog.policy_proposal
    ADD COLUMN share_overrides jsonb NOT NULL DEFAULT '[]'::jsonb
        CHECK (jsonb_typeof(share_overrides) = 'array');
