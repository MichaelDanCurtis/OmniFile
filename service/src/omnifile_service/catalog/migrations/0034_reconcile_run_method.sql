-- A reconcile run records how it read the backend (reconcile/run.py):
--   inventory           a Blob Inventory report (reconcile/blob_inventory.py)
--   listing             a non-WORM container listed: no report configured, or
--                       none usable (method_detail says which and why), or a
--                       bucket's source prefixes
--   listing_changefeed  a WORM container: the change feed, then a listing
-- Before this migration a non-WORM run was always recorded `inventory`, even
-- when it listed; those rows keep it, with no detail.
ALTER TABLE catalog.reconcile_run DROP CONSTRAINT reconcile_run_method_check;
ALTER TABLE catalog.reconcile_run ADD CONSTRAINT reconcile_run_method_check
    CHECK (method IN ('inventory', 'listing', 'listing_changefeed'));
-- What was read (the report's manifest), or why no report was.
ALTER TABLE catalog.reconcile_run ADD COLUMN method_detail text;
