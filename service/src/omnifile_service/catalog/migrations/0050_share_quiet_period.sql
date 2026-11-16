-- Migration 0050: each share's quiet period (decision 67).
--
-- How long a file on the share must sit untouched before the edge treats it as
-- finished. NULL means the share's default: the edge's `[commit] worm_quiet_s`
-- on a WORM share (60 s), its `[commit] quiet_s` on any other (10 s). Existing
-- shares stay NULL, so a WORM share moves to 60 s when its edge is upgraded and
-- every other share keeps today's period. Nothing is written here.
--
-- 10 s is the floor: below today's period a pause of under 20 s would commit a
-- partial version, which an ordinary NFS writeback stall can reach. 600 s is
-- the ceiling: the commit's further wait gives up after about 18 minutes of
-- looking (edge `carry.rs`), and the detector holds an open file for each file
-- it waits on. The edge has the same bounds (`quiet.rs`), and so does
-- `PATCH /v1/shares/{share_id}/settings`.

ALTER TABLE catalog.share
    ADD COLUMN quiet_s integer
        CONSTRAINT share_quiet_range CHECK (quiet_s IS NULL OR quiet_s BETWEEN 10 AND 600);
