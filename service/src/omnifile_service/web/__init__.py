"""The `web` app: the portal's JSON API and the partner API (docs/contracts/portal-api.md).

- `auth`: Entra OIDC sign-in as a backend-for-frontend (sessions in the
  catalog), CSRF, re-authentication, and partner bearer tokens.
- `devidp`: a dev OIDC issuer, mounted only with OMNIFILE_DEV_MODE=1.
- `access`: decision 28: every item is checked with catalog.acl_can_read
  against the caller's SIDs; unreadable items are 404.
- `routes_*`: the endpoints of portal-api.md section 10.
- `preview`: the seam to the Policy preview track (policy/preview.py,
  cost.py, checks.py).
- `static`: the built SPA, with a strict CSP.
"""
