"""Teams cards and email for recalls (docs/contracts/teams.md).

- `intents`: Ed25519 card intents, the relay's format byte for byte.
- `cards`: the one card renderer, `render(state, audience, intents)`, and the email body.
- `graph`: Microsoft Graph (SID -> user, groups, proactive install, sendMail).
- `botconnector`: Bot Connector REST (send and update a card).
- `tokens`: client-credentials and managed-identity access tokens.
- `facts`: the catalog facts a card shows, shared by `api` and the worker.
- `delivery`: the `notify` consumer's work, per recall.
- `fakes/`: fake Graph, fake Bot Connector, and a fake Teams client.
"""
