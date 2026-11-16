-- Migration 0058: a refused copy can be stopped and acknowledged (#81's follow-up,
-- annex.md section 3.3).
--
-- At `MAX_REFUSALS` (annex/readback.py) the service stops asking an annex's edge to copy
-- a file whose copy Azure's read-back keeps refusing, but the edge's own reconciliation,
-- every `reconcile_days`, still copied it again: with `reconcile_days` at 1, a big file
-- was uploaded again each day until the backend was fixed. And nothing could quiet the
-- alert `annex-copies-refused` for one file. Now, per file, an ArchiveAdmin, with a fresh
-- sign-in and audited:
--
-- - copy_stopped_at, copy_stopped_by: "stop copying this file again until I say". The
--   edge's records say `copy_stopped` (`GET annexes/{id}/records`), and its
--   reconciliation doesn't copy the same bytes again. A changed file on the NAS is
--   still copied: that is a new copy, not the refused one again.
-- - copy_refused_ack_at, copy_refused_ack_by: an acknowledgement that takes this file
--   out of the gauge `omnifile.annex.copies_refused`, so the alert quiets for it only.
--
-- Both are cleared when a version of the file verifies (the trigger below, and a copy
-- already verified when the edge reports it, api/annex.py), and the stop when the admin
-- says so. A stopped file stays on the annex's Problems until then, however old.
ALTER TABLE catalog.annex_file
    ADD COLUMN copy_stopped_at timestamptz,
    ADD COLUMN copy_stopped_by text,
    ADD COLUMN copy_refused_ack_at timestamptz,
    ADD COLUMN copy_refused_ack_by text,
    ADD CONSTRAINT annex_file_copy_stopped CHECK ((copy_stopped_at IS NULL) = (copy_stopped_by IS NULL)),
    ADD CONSTRAINT annex_file_copy_refused_ack CHECK (
        (copy_refused_ack_at IS NULL) = (copy_refused_ack_by IS NULL));

CREATE OR REPLACE FUNCTION catalog.annex_location_verified() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    v_annex bigint;
    v_path  text;
BEGIN
    UPDATE catalog.annex_file af
       SET verified_version_id = NEW.version_id, verified_at = NEW.verified_at,
           copy_refusals = 0, copy_refused_version_id = NULL,
           copy_stopped_at = NULL, copy_stopped_by = NULL,
           copy_refused_ack_at = NULL, copy_refused_ack_by = NULL, updated_at = now()
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
