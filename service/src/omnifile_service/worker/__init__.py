"""The OmniFile worker: PGMQ consumers and periodic policy sweeps (M1).

- `queue`: the consumer framework (visibility timeout, retries, archive on
  success, dead-letter after N failures).
- `backends`: `catalog.backend` row -> `StorageBackend` (sim or Azure).
- `stubs`: the M1 "stub after N minutes" policy and the `edge_instructions`
  handler.
- `recalls`: the `restores` handler (start-or-join a provider restore, poll).
- `runner`: wires them together; ``python -m omnifile_service.worker``.
"""

from .backends import BackendRegistry, BackendRow, backend_for
from .queue import Consumer, ConsumerConfig, Message, Permanent, Retry, send

__all__ = [
    "BackendRegistry",
    "BackendRow",
    "Consumer",
    "ConsumerConfig",
    "Message",
    "Permanent",
    "Retry",
    "backend_for",
    "send",
]
