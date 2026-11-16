-- Migration 0051 (it was 0049, then 0050, until #77 and #80 took those numbers).
--
-- A copy that Azure's read-back refused (worker/verify.py) stays unverified, and the
-- edge copies it again only at its annex's next reconciliation, up to `reconcile_days`
-- away. Now the service asks for one sooner, a bounded number of times (annex/readback.py,
-- `annex.md` section 3.3):
--
-- - annex.reconcile_asked_at: when the service asked the annex's edge to reconcile. It
--   may be ahead of now (a backoff); `GET annexes` shows it only once it has come. The
--   edge reconciles when it sees a value it hasn't answered, and then copies again only
--   the files whose records say `refused`.
-- - annex_file.copy_refusals: read-backs refused in a row, one per version, back to 0
--   when a version verifies. At `MAX_REFUSALS` the service stops asking, and a gauge
--   (`omnifile.annex.copies_refused`) keeps an alert up.
-- - copy_refused_version_id, copy_refused_at: the last version refused, and when.
-- - A verified version also resolves the file's `copy_mismatch` drift (before, the
--   next `copied` event did, before that copy's own read-back).
ALTER TABLE catalog.annex ADD COLUMN reconcile_asked_at timestamptz;

ALTER TABLE catalog.annex_file
    ADD COLUMN copy_refusals integer NOT NULL DEFAULT 0 CHECK (copy_refusals >= 0),
    ADD COLUMN copy_refused_version_id bigint,
    ADD COLUMN copy_refused_at timestamptz;

CREATE OR REPLACE FUNCTION catalog.annex_location_verified() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    v_annex bigint;
    v_path  text;
BEGIN
    UPDATE catalog.annex_file af
       SET verified_version_id = NEW.version_id, verified_at = NEW.verified_at,
           copy_refusals = 0, copy_refused_version_id = NULL, updated_at = now()
     WHERE af.file_id = NEW.file_id
       AND af.copied_version_id = NEW.version_id
     RETURNING af.annex_id, af.path INTO v_annex, v_path;
    IF v_annex IS NOT NULL THEN
        INSERT INTO catalog.annex_event (annex_id, kind, path, file_id, version_id, size, details)
        VALUES (v_annex, 'verified', v_path, NEW.file_id, NEW.version_id, NEW.size,
                jsonb_build_object('verified_by', NEW.verified_by,
                                   'crc64nvme', encode(NEW.crc64nvme, 'hex')));
        UPDATE catalog.reconcile_drift d
           SET last_seen_at = now(),
               observed = coalesce(d.observed, '{}'::jsonb)
                          || jsonb_build_object('resolved_by', 'verified')
          FROM catalog.annex a
          JOIN catalog.source s ON s.source_id = a.source_id
          JOIN catalog.filer fl ON fl.filer_id = a.filer_id
         WHERE a.annex_id = v_annex
           AND d.source = 'annex' AND d.kind = 'copy_mismatch' AND d.resolved_at IS NULL
           AND d.backend_id = s.backend_id AND d.container = fl.export
           AND d.object_key = CASE WHEN a.path = '' THEN v_path ELSE a.path || '/' || v_path END;
    END IF;
    RETURN NULL;
END $$;
