-- Migration 0011: Teams cards and email (M4; docs/contracts/teams.md).
--
-- 1. identity: the user's mail address and the outcome of the last Graph
--    lookup (resolved | not_found | error), with a retry time for backoff.
-- 2. teams_conversation: per identity, the bot's 1:1 chat (from the
--    proactive install) or `blocked` when the tenant refused the install.
-- 3. notification: audience (requester | owner | watcher), Track, the card
--    version on screen, what that card showed, and when to try again.
--    State `skipped`: nobody can be told (no access, no mail).
-- 4. teams_intent: revoked when a newer card replaces its buttons, and the
--    card version that carried it.
-- 5. An index for the notify sweep (due retries).
--
-- Applies after 0010 (which adds identity.groups_source and the
-- identity_resolve queue); it uses none of 0010's objects.

-- ---------------------------------------------------------------------------
-- 1. Identity: Graph lookups (teams.md section 7)
-- ---------------------------------------------------------------------------
ALTER TABLE catalog.identity
    ADD COLUMN mail                  text,
    ADD COLUMN graph_state           text CHECK (graph_state IN ('resolved', 'not_found', 'error')),
    -- An error code, never a response body.
    ADD COLUMN graph_error           text,
    -- not_found: re-checked after 24 h; error: backoff.
    ADD COLUMN graph_next_attempt_at timestamptz;

-- ---------------------------------------------------------------------------
-- 2. The bot's conversation with each user (teams.md section 6.2)
-- ---------------------------------------------------------------------------
CREATE TABLE catalog.teams_conversation (
    identity_id      bigint PRIMARY KEY REFERENCES catalog.identity,
    -- blocked: Graph refused the install (tenant app policy, no Teams
    -- licence). The user gets email instead until the row is cleared.
    state            text NOT NULL DEFAULT 'active' CHECK (state IN ('active', 'blocked')),
    conversation_id  text,
    -- Never taken from a click: OMNIFILE_TEAMS_SERVICE_URL at install time.
    service_url      text,
    install_id       text,
    installed_at     timestamptz,
    last_error       text,
    updated_at       timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT teams_conversation_active
        CHECK (state <> 'active' OR (conversation_id IS NOT NULL AND service_url IS NOT NULL))
);

-- ---------------------------------------------------------------------------
-- 3. Notifications (teams.md sections 3.1 and 6.5)
-- ---------------------------------------------------------------------------
ALTER TABLE catalog.notification
    ADD COLUMN audience        text NOT NULL DEFAULT 'requester'
                               CHECK (audience IN ('requester', 'owner', 'watcher')),
    ADD COLUMN tracking        boolean NOT NULL DEFAULT false,
    -- Bumped on every send or update; intents of older versions are revoked.
    ADD COLUMN card_version    integer NOT NULL DEFAULT 0 CHECK (card_version >= 0),
    -- What the card on screen shows besides the state (hold, tracking,
    -- access), so a click that changes only those still updates it.
    ADD COLUMN last_card_key   text,
    ADD COLUMN last_sent_at    timestamptz,
    ADD COLUMN next_attempt_at timestamptz,
    -- First failure of the current run of failures; after 1 h, email.
    ADD COLUMN failing_since   timestamptz;

ALTER TABLE catalog.notification DROP CONSTRAINT notification_state_check;
ALTER TABLE catalog.notification ADD CONSTRAINT notification_state_check
    CHECK (state IN ('queued', 'sent', 'failed', 'fell_back', 'skipped'));

-- ---------------------------------------------------------------------------
-- 4. Intents replaced by a newer card (teams.md section 5, step 6)
-- ---------------------------------------------------------------------------
ALTER TABLE catalog.teams_intent
    ADD COLUMN revoked_at   timestamptz,
    ADD COLUMN card_version integer;
-- Revoking a notification's unused intents, and finding them for a reply.
CREATE INDEX teams_intent_notification
    ON catalog.teams_intent (notification_id) WHERE used_at IS NULL AND revoked_at IS NULL;

-- ---------------------------------------------------------------------------
-- 5. The notify sweep: notifications waiting for a retry or a coalesced update
-- ---------------------------------------------------------------------------
CREATE INDEX notification_due
    ON catalog.notification (next_attempt_at)
    WHERE next_attempt_at IS NOT NULL AND state IN ('queued', 'sent', 'failed');
