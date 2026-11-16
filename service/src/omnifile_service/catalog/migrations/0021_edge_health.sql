-- Edge health from the heartbeat (control-api.md section 4.7; decision 37,
-- docs/design/observability.md section 2). The heartbeat already updated
-- last_seen_at and software_version; it now also keeps what the edge
-- reports about itself, so the portal and the alert rules can see a
-- disarmed guard, a growing outbox or a full data volume without anything
-- listening on the appliance. `health` holds the gauges as sent (bytes and
-- counts); `feed_lag_events` is the service's own count at that beat.

ALTER TABLE catalog.edge
    ADD COLUMN last_heartbeat_at timestamptz,
    ADD COLUMN kernel            text,
    ADD COLUMN guard_state       text CHECK (guard_state IN ('armed', 'degraded', 'down')),
    ADD COLUMN health            jsonb NOT NULL DEFAULT '{}'::jsonb;
