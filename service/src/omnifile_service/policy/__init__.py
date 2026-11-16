"""The policy engine and tiering scheduler (M3, ARCHITECTURE.md section 5).

- `model`: the policy body (mode, clock, days, stub_at) and the editor's checks.
- `engine`: one file against its policy, tier pin and state -> a `Plan`.
- `recall`: restore speed, the per-account 10 GiB/h High budget, ETAs (no
  promise above 10 GB) and the wait/fail (waitquick) decision.
- `scheduler`: the catalog sweep that turns plans into queued work.

Execution lives in the worker: `worker.tiering` (moves, budgeted restores)
and `worker.tiering_jobs` (registration).
"""

from .engine import FileFacts, Pin, Plan, evaluate
from .model import Mode, Policy, PolicyClock, PolicyError, Problem, StubAt, problems
from .recall import RecallPlan, decide_on_open, fsal_open_policy, plan_recall

__all__ = [
    "FileFacts",
    "Mode",
    "Pin",
    "Plan",
    "Policy",
    "PolicyClock",
    "PolicyError",
    "Problem",
    "RecallPlan",
    "StubAt",
    "decide_on_open",
    "evaluate",
    "fsal_open_policy",
    "plan_recall",
    "problems",
]
