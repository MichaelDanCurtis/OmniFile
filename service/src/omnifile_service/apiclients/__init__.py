"""API clients: who may call the API with an app credential (portal-api.md 2.2, 5.21).

The owner's rules (2026-11-04):

- **Registered and enabled, besides the token.** A request with an Entra app
  token is accepted only if its application (``azp``) is registered here and
  enabled, on top of its signature and app role. Its roles are cut to the
  client's scopes.
- **Every client has an owner**, a person in the tenant. Each proposal and
  action records the client and its owner (`Principal.api_client`, the audit
  rows' details). A proposal is refused when its owner can't see the dataset
  it targets: ArchiveAdmin, DataSteward, or Owner of one of its shares
  (`access.owner_can_see_dataset`).
- **No keys by default.** A client authenticates with its own Entra app
  credential, a certificate by preference. OmniFile-issued API keys are the
  alternative (`keys.py`): shown once, kept as a hash, with an expiry, and
  bound to the client and its owner, whose rules they follow.
- A client is disabled at once (its next call is refused) and can be revoked.
"""
