"""Versioning depth and WORM (decisions 30 and 31), M3.

- `depth`: rewind depth (Owners, within the ArchiveAdmin maximum) and pruning
  beyond it.
- `worm`: WORM per share, catalog side: enable (typed confirmation, one-way
  once locked), auto-lock after the validation window, extend-only
  retention, legal holds.
- `worm_sync`: the backend side: container policy, per-version retention,
  legal hold and state tags, deletes of pruned bytes, recall-copy expiry.
- `policy_admin`: the container-policy (management-plane) seam.

The worker runs them through `worker/retention_jobs.py`.
"""

from .common import RetentionError
from .depth import PruneResult, lifecycle_policy, prune_versions, set_rewind_depth, set_rewind_max
from .worm import (
    cancel_worm_test,
    clear_legal_hold,
    enable_worm,
    extend_worm_retention,
    lock_due_shares,
    set_legal_hold,
)
from .worm_sync import delete_blob, expire_recall_copies, stamp_worm, sync_share_policy

__all__ = [
    "PruneResult",
    "RetentionError",
    "cancel_worm_test",
    "clear_legal_hold",
    "delete_blob",
    "enable_worm",
    "expire_recall_copies",
    "extend_worm_retention",
    "lifecycle_policy",
    "lock_due_shares",
    "prune_versions",
    "set_legal_hold",
    "set_rewind_depth",
    "set_rewind_max",
    "stamp_worm",
    "sync_share_policy",
]
