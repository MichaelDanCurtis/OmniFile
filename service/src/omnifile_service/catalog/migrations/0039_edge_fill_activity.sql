-- Migration 0039: when an edge last reported a fill running (control-api.md
-- section 4.4, "An edge that stops"; worker/recall_timeout.py).
--
-- A recall in `downloading` waits for its edge to fill the file. If that edge
-- is gone for good, nothing else would ever end the recall: every later read
-- joins it and the card says "Copying to the file server" forever. The worker
-- fails such a recall, but only on evidence that the edge is not working on
-- it, never on how long the fill has taken (a 1 TB file over a slow link
-- takes days):
--
-- - the edge has not been heard from (no heartbeat) for a while, or
-- - it heartbeats, reports its running fills, and has reported none for a
--   while since the recall reached `downloading`.
--
-- The heartbeat's `fills_running` gauge is kept in `health` as sent; this
-- column records when it was last above zero.

ALTER TABLE catalog.edge ADD COLUMN last_fill_at timestamptz;
