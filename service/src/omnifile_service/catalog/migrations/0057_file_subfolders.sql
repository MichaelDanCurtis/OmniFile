-- migrate: no-transaction
-- Migration 0057: a folder's subfolders, without its files.
--
-- The share map walks a share's folders (web/routes_visual.py, readable_dirs).
-- With only (parent_id, name), each step read every file in a folder to find its
-- subfolders: at 10^7 files that was most of a live count's 45 to 122 s even with
-- the captured ACL alone (results/wp14-share-map.md). Built CONCURRENTLY, so edge
-- commits go on while it builds.

CREATE INDEX CONCURRENTLY IF NOT EXISTS file_subfolders ON catalog.file (parent_id, name) WHERE kind = 'dir' AND deleted_at IS NULL;
