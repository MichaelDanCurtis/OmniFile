"""The advisory lock that names one blob, whichever file refers to it.

The tiering worker holds it exclusively across an in-place tier change and its
record; a dedupe holds it shared while it copies a location's tier into a new
reference. A hash collision only makes two blobs take turns.
"""

from __future__ import annotations


def blob_lock_key(backend_id: int, container: str, key: str) -> str:
    return f"omnifile:blob-tier:{backend_id}/{container}/{key}"
