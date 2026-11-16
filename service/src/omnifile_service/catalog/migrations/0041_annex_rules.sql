-- Migration 0041: Annex phase A2.1, rules and dry runs (docs/design/annex.md
-- sections 4.1-4.5; docs/milestones/results/wp14-a2.md).
--
-- Rule sets become real if-then rules (`annex/rules.py` checks them; the
-- edge evaluates them, for the preview and for real). Modes Archive after N
-- days and Move now are now creatable, and their rule sets need an
-- ArchiveAdmin's approval. **Nothing is removed from a NAS in A2.1**: a rule
-- that removes produces `would_remove` events only, and the settings the
-- removal pipeline (A2.2) will obey are recorded here so the portal can show
-- them: the pause switches, the dry-run end, the rate, the window, the grace
-- period and pointer files.
--
-- - `catalog.annex`: modes `archive_after`, `move_now`, `custom`; the
--   removal settings.
-- - `catalog.annex_ruleset`: the proposer's rationale, whether the proposal
--   removes and so needs an ArchiveAdmin, and the dry run it was proposed
--   with.
-- - `catalog.annex_file.rule` (0038) now names the rule that decided the file.
-- - `catalog.annex_dry_run`: a preview asked for in the portal and answered
--   by the edge (control API section 4.8).
-- - `catalog.annex_defaults`: the global pause, and the removal defaults.

ALTER TABLE catalog.annex DROP CONSTRAINT annex_mode_check;
ALTER TABLE catalog.annex ADD CONSTRAINT annex_mode_check
    CHECK (mode IN ('copy', 'archive_after', 'move_now', 'custom'));
ALTER TABLE catalog.annex
    ADD COLUMN removals_paused       boolean NOT NULL DEFAULT false,
    -- Removing rule sets run in dry run until then (decision 5: 7 days),
    -- and A2.2 removes only once an ArchiveAdmin confirms the dry run.
    ADD COLUMN dry_run_until         timestamptz,
    ADD COLUMN removal_rate_per_hour integer NOT NULL DEFAULT 1000
                                     CHECK (removal_rate_per_hour BETWEEN 1 AND 100000),
    -- {"start": "HH:MM", "end": "HH:MM", "tz": "Area/City"}, or none.
    ADD COLUMN removal_window        jsonb,
    ADD COLUMN pointer_files         boolean NOT NULL DEFAULT false,
    ADD COLUMN grace_minutes         integer NOT NULL DEFAULT 15
                                     CHECK (grace_minutes BETWEEN 1 AND 1440);

ALTER TABLE catalog.annex_ruleset
    ADD COLUMN rationale       text CHECK (length(rationale) <= 2000),
    -- Adds or widens a removal: an ArchiveAdmin approves it (section 4.3).
    ADD COLUMN removes         boolean NOT NULL DEFAULT false,
    ADD COLUMN needs_archive_admin boolean NOT NULL DEFAULT false;

CREATE TABLE catalog.annex_dry_run (
    run_id          bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    annex_id        bigint NOT NULL REFERENCES catalog.annex,
    -- The proposal it previews, if any (a draft from the builder has none).
    ruleset_version integer,
    body            jsonb NOT NULL,
    -- requested -> running (the edge took it) -> done | failed; a run not
    -- taken or answered in time fails (annex/dry_runs.py).
    state           text NOT NULL DEFAULT 'requested'
                    CHECK (state IN ('requested', 'running', 'done', 'failed')),
    requested_by    text NOT NULL,
    requested_at    timestamptz NOT NULL DEFAULT now(),
    taken_at        timestamptz,
    done_at         timestamptz,
    report          jsonb,
    error           text,
    CONSTRAINT annex_dry_run_done CHECK ((state IN ('requested', 'running')) = (done_at IS NULL)),
    CONSTRAINT annex_dry_run_taken CHECK (state <> 'running' OR taken_at IS NOT NULL),
    FOREIGN KEY (annex_id, ruleset_version) REFERENCES catalog.annex_ruleset (annex_id, version)
);
CREATE INDEX annex_dry_run_waiting ON catalog.annex_dry_run (annex_id)
    WHERE state IN ('requested', 'running');
ALTER TABLE catalog.annex_ruleset
    ADD COLUMN dry_run_id bigint REFERENCES catalog.annex_dry_run (run_id);

ALTER TABLE catalog.annex_defaults
    -- Settings, "pause all removals": no annex removes while it is on.
    ADD COLUMN removals_paused       boolean NOT NULL DEFAULT false,
    ADD COLUMN removal_rate_per_hour integer NOT NULL DEFAULT 1000
                                     CHECK (removal_rate_per_hour BETWEEN 1 AND 100000),
    ADD COLUMN grace_minutes         integer NOT NULL DEFAULT 15
                                     CHECK (grace_minutes BETWEEN 1 AND 1440),
    ADD COLUMN dry_run_days          integer NOT NULL DEFAULT 7
                                     CHECK (dry_run_days BETWEEN 1 AND 90);
