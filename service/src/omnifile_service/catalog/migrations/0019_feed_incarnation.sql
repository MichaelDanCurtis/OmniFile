-- Feed incarnations (deploy/azure/README.md, "Catalog backup and restore").
-- Feed cursors are transaction ids. A point-in-time restore of the catalog
-- hands out again the transaction ids its lost timeline used, so a cursor
-- an edge or partner kept from before the restore names events that no
-- longer exist, and would skip new ones that reuse those ids, silently.
-- catalog.feed_restored(), run once after a restore, starts a new
-- incarnation: cursors carry it (from 1 on, as a third part), and one from
-- another incarnation is refused (reset_required, 410 cursor_expired), so
-- its reader takes a new snapshot. "0.0" (everything kept) stays valid.

ALTER TABLE catalog.feed_retention ADD COLUMN incarnation integer NOT NULL DEFAULT 0;

--
-- Identifiers come from sequences, which the restore rewinds too: a file id
-- an edge still holds for a file of the lost timeline would be handed to a
-- new file. So feed_restored() also moves every catalog sequence 2^40 past
-- where it is, more than the lost timeline can have used.

CREATE FUNCTION catalog.feed_restored() RETURNS integer
LANGUAGE plpgsql VOLATILE AS $$
DECLARE
    s record;
    inc integer;
BEGIN
    FOR s IN SELECT c.oid::regclass AS seq
               FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
              WHERE c.relkind = 'S' AND n.nspname = 'catalog' LOOP
        PERFORM setval(s.seq, nextval(s.seq) + (1::bigint << 40));
    END LOOP;
    UPDATE catalog.feed_retention SET incarnation = incarnation + 1
        RETURNING incarnation INTO inc;
    RETURN inc;
END $$;
