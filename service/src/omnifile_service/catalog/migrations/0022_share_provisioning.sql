-- Adding datasets and shares in the portal (M6; docs/design/share-provisioning.md).
-- The edge renders each share's Samba stanza and Ganesha EXPORT block itself,
-- from its share.upsert rows, so the catalog carries what the render needs:
--
-- - nfs_export_id: Ganesha's Export_Id. NFS clients keep it in their file
--   handles, so it never changes for a share and is never reused on its edge
--   while the share exists. Assigned on insert: the next free id on the edge,
--   from 2 (0 and 1 are Ganesha's). Unique per edge.
-- - smb_encrypt: SMB3 encryption required (`smb encrypt = required`).
-- - share names are unique per edge regardless of case.

ALTER TABLE catalog.share
    ADD COLUMN nfs_export_id integer CHECK (nfs_export_id BETWEEN 2 AND 65535),
    ADD COLUMN smb_encrypt   boolean NOT NULL DEFAULT false;

UPDATE catalog.share s
   SET nfs_export_id = n.id
  FROM (SELECT share_id, 1 + row_number() OVER (PARTITION BY edge_id ORDER BY share_id) AS id
          FROM catalog.share) n
 WHERE n.share_id = s.share_id;

ALTER TABLE catalog.share
    ALTER COLUMN nfs_export_id SET NOT NULL,
    ADD CONSTRAINT share_export_id_uq UNIQUE (edge_id, nfs_export_id);

-- Samba and Windows match share names regardless of case: "Data" and "data"
-- on one edge would be one SMB share with two definitions.
CREATE UNIQUE INDEX share_name_ci_uq ON catalog.share (edge_id, lower(name));

CREATE FUNCTION catalog.share_export_id() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.nfs_export_id IS NULL THEN
        -- One insert at a time per edge, so two shares can't take the same id.
        PERFORM pg_advisory_xact_lock(
            hashtextextended('catalog.share_export_id:' || NEW.edge_id, 0));
        SELECT min(c) INTO NEW.nfs_export_id
          FROM generate_series(2, 65535) c
         WHERE NOT EXISTS (SELECT 1 FROM catalog.share
                            WHERE edge_id = NEW.edge_id AND nfs_export_id = c);
        IF NEW.nfs_export_id IS NULL THEN
            RAISE EXCEPTION 'no free NFS export id on edge %', NEW.edge_id;
        END IF;
    END IF;
    RETURN NEW;
END $$;

CREATE TRIGGER share_export_id BEFORE INSERT ON catalog.share
    FOR EACH ROW EXECUTE FUNCTION catalog.share_export_id();

-- An export id belongs to its share for good.
CREATE FUNCTION catalog.share_export_id_fixed() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.nfs_export_id IS DISTINCT FROM OLD.nfs_export_id THEN
        RAISE EXCEPTION 'a share''s NFS export id never changes';
    END IF;
    RETURN NEW;
END $$;

CREATE TRIGGER share_export_id_fixed BEFORE UPDATE OF nfs_export_id ON catalog.share
    FOR EACH ROW EXECUTE FUNCTION catalog.share_export_id_fixed();
