"""OmniFile Gateway service.

Planned: edge control API, partner API for Prizmanyx, catalog (Postgres), policy engine,
tiering scheduler, recall orchestrator, storage backends (Azure Blob, Amazon S3), Teams notifier.
See docs/ARCHITECTURE.md. Nothing is built yet.
"""

import importlib.metadata

try:
    # The installed distribution's version: its pyproject.toml, which
    # release/stamp.sh writes from VERSION.
    __version__ = importlib.metadata.version("omnifile-service")
except importlib.metadata.PackageNotFoundError:  # imported from a source tree that isn't installed
    __version__ = "0+unknown"
