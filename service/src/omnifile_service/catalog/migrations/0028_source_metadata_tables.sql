-- Migration 0028: S3 Metadata tables as a source's inventory and journal
-- (docs/design/catalog-seeding.md sections 7 and 8; sources/metadata_s3.py).
--
-- `inventory` may now also be {"kind": "s3_metadata", "table_bucket_arn",
-- "namespace"?, "table"?, "journal_table"?} (seed from the live inventory
-- table, follow the journal table) or {"kind": "listing"} (list the prefix,
-- chosen explicitly; NULL lists it too unless the bucket's backend names its
-- Metadata tables).

ALTER TABLE catalog.source DROP CONSTRAINT source_inventory_kind;
ALTER TABLE catalog.source ADD CONSTRAINT source_inventory_kind
    CHECK (inventory IS NULL
           OR inventory->>'kind' IN ('s3_inventory', 's3_metadata', 'listing'));

-- An `s3_metadata` source's position in its journal table once it is seeded
-- (worker/journal_sync.py): {"snapshot_id", "through"}, plus {"to", "after"}
-- while a read is applied in batches. NULL: from `snapshot_at`.
ALTER TABLE catalog.source ADD COLUMN journal jsonb;
