"""The portal's Settings area (portal-api.md 5.20; the owner's request of 2026-11-04).

Settings **connect and check; they never store a secret.** Azure is reached
with managed identities and RBAC, AWS with web-identity federation (decision
43), Teams with the bot's managed identity. So each page shows what the
deployment configured, runs live checks against it, and says what to fix:

- `storage.py`: backends (added and edited through `admin/backends.py`, the
  CLI's code path) and their checks: private DNS (decision 22), the RBAC role,
  the live/archive pair and change feed, S3's account and role;
- `aws.py`: the trust policy a role needs for the service's identity;
- `identity.py`: the tenant, the app roles and their groups, admin consent;
- `notify.py`: Teams and email health from the catalog's notifications;
- `mover.py`: Storage Mover agents and projects through ARM;
- `alerts.py`: the alert rules of observability.md, with the catalog's view;
- `tests.py`: test sends (a card, an email, an AWS role assumption), which the
  worker runs because it holds those identities (`worker_jobs.py`).

Outbound calls go through `ports.Probes`, so tests use fakes and never reach
Azure, AWS, Graph or Teams.
"""
