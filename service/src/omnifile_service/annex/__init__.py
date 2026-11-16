"""Annexes: OmniFile as a storage sync for an existing NAS (docs/design/annex.md,
decision 51). Phase A1: Copy mode.

- `acl`: ACLs captured from the NAS, reduced to who may read.
- `rules`: the fixed rule templates (A1: Copy only).
- `purge`: the worker's purge of a copy of a file deleted on the NAS.

The edge-facing routes are `api/annex.py`; the portal's are `web/routes_annex.py`.
"""
