-- Migration 0053 for now (after decision 63's 0052): it moves to the next free number
-- at merge.
--
-- Decision 65: filer setup asks whether a share or export limits access beyond its
-- file permissions. SMB share permissions and NFS export rules can't be read through a
-- normal mount, so an ArchiveAdmin answers for them (annex.md section 5):
--
-- - catalog.filer.access_limited: NULL until answered; true when the share or export
--   lets in only some AD groups (`access_groups`), false when it doesn't limit access.
-- - access_confirmed_by, access_confirmed_at: who answered, and when; the portal shows
--   it, and the answer is audited with their name (`filer.access`).
-- - catalog.filer_admits(filer, user, groups): the filer's question has been
--   answered, and when access is limited the user or one of its groups is in
--   `access_groups`. Unanswered keeps everyone out (fail closed, as decision 63's
--   folders do).
-- - catalog.passable_gates: a gate (decision 63, per filer) is passed only where its
--   filer admits the user too, so catalog.can_read_file is the captured ACL, the
--   folders above the file, and the filer's answer, for annexed files and the
--   annex's catalog folders.
-- - A problem on every live annex whose filer hasn't been answered.

ALTER TABLE catalog.filer
    ADD COLUMN access_limited boolean,
    ADD COLUMN access_groups catalog.sid[] NOT NULL DEFAULT '{}',
    ADD COLUMN access_confirmed_by text,
    ADD COLUMN access_confirmed_at timestamptz,
    ADD CONSTRAINT filer_access_shape CHECK (
        (access_limited IS NULL AND access_confirmed_at IS NULL)
        OR (access_limited IS NOT NULL AND access_confirmed_at IS NOT NULL
            AND access_confirmed_by IS NOT NULL
            AND (access_limited = (cardinality(access_groups) > 0))));

-- Whether a filer's answer lets the user in. Unanswered: nobody.
CREATE FUNCTION catalog.filer_admits(p_filer bigint, p_user text, p_groups text[])
RETURNS boolean
LANGUAGE sql STABLE PARALLEL SAFE AS $$
    SELECT coalesce((
        SELECT CASE
                   WHEN fl.access_limited IS NULL THEN false
                   WHEN NOT fl.access_limited THEN true
                   ELSE p_user = ANY (fl.access_groups::text[])
                        OR coalesce(p_groups, '{}') && fl.access_groups::text[]
               END
          FROM catalog.filer fl WHERE fl.filer_id = p_filer), false)
$$;

-- Decision 63's gates are per filer (migration 0052), so the filer's answer is one
-- more condition on a gate, worked out once per query with the folders': an
-- annexed file or catalog folder costs nothing more per row.
CREATE OR REPLACE FUNCTION catalog.passable_gates(p_user text, p_groups text[])
RETURNS bigint[]
LANGUAGE sql STABLE PARALLEL SAFE AS $$
    SELECT coalesce(array_agg(g.gate_id), '{}') FROM catalog.read_gate g
     WHERE catalog.filer_admits(g.filer_id, p_user, p_groups)
       AND NOT EXISTS (
        SELECT 1 FROM unnest(g.acl_ids) AS i (acl_id)
          LEFT JOIN catalog.acl s ON s.acl_id = i.acl_id
         WHERE NOT coalesce(catalog.acl_can_read(s.search_sids, s.search_deny_sids,
                                                 p_user, p_groups, s.deny_first), false))
$$;

INSERT INTO catalog.annex_event (annex_id, kind, details)
SELECT a.annex_id, 'problem',
       jsonb_build_object(
           'reason', 'filer_access_unanswered',
           'detail', 'Files are hidden from everyone until an ArchiveAdmin answers, on the filer,'
                     ' whether its share or export limits access beyond the files'' permissions'
                     ' (decision 65).')
  FROM catalog.annex a
 WHERE a.state <> 'retired';
