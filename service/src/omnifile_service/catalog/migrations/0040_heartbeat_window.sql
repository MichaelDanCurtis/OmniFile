-- Migration 0040: the service's reachable window (worker/recall_timeout.py).
--
-- The worker fails a `downloading` recall whose edge has not been heard from
-- for a while. An outage of the service, or of the edges' way to it (its
-- ingress, allow-list or certificates, or the api losing the catalog), looks
-- the same from the catalog: no edge can heartbeat. So silence counts only
-- within the current window in which heartbeats have kept arriving from the
-- edges, with no gap over about 3 minutes (edges heartbeat every minute).
-- Every heartbeat keeps this one row: `last_at` is the latest heartbeat, and
-- `started_at` moves to now when the gap since the previous one was longer.

CREATE TABLE catalog.heartbeat_window (
    id          boolean PRIMARY KEY DEFAULT true CHECK (id),
    started_at  timestamptz NOT NULL DEFAULT now(),
    last_at     timestamptz NOT NULL DEFAULT now()
);
