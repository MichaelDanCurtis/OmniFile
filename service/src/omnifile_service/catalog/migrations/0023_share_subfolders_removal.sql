-- Shares of subfolders, and removing a share (M6; docs/design/share-provisioning.md).
--
-- - A folder is the root of at most one share (a dataset's root already was,
--   checked by the portal routes).
-- - Removing a share removes only the share: the dataset, its files and its
--   versions stay. The edge stops serving it when it gets `share.delete`.
--   Its per-share usage rows go with it.

CREATE UNIQUE INDEX share_root_dir_uq ON catalog.share (root_dir_id) WHERE root_dir_id IS NOT NULL;

ALTER TABLE catalog.usage_snapshot
    DROP CONSTRAINT usage_snapshot_share_id_fkey,
    ADD CONSTRAINT usage_snapshot_share_id_fkey
        FOREIGN KEY (share_id) REFERENCES catalog.share ON DELETE CASCADE;

-- The edge serving the share hears of its removal. The version (one past the
-- share's last) rides in `detail`, as the feed reader returns no version.
CREATE FUNCTION catalog.share_feed_delete() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    INSERT INTO catalog.feed_event (dataset_id, edge_id, entity, entity_id, entity_version, op,
                                    origin_edge_id, origin_op_id, detail)
    VALUES (OLD.dataset_id, OLD.edge_id, 'share', OLD.share_id, OLD.entity_version + 1, 'delete',
            catalog.feed_origin_edge(), catalog.feed_origin_op(),
            jsonb_build_object('entity_version', OLD.entity_version + 1, 'name', OLD.name));
    RETURN NULL;
END $$;

CREATE TRIGGER share_feed_delete AFTER DELETE ON catalog.share
    FOR EACH ROW EXECUTE FUNCTION catalog.share_feed_delete();
