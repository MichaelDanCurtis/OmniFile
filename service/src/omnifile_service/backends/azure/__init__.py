"""The real Azure Blob backend (M1). See `backend.py` for the rules it follows."""

from .backend import (
    AzureBlobBackend,
    from_azure_metadata,
    normalize_posix,
    posix_metadata,
    to_azure_metadata,
)
from .config import AzureAccount, AzureBlobConfig
from .errors import AccessDenied, ChecksumMismatch, Unavailable

__all__ = [
    "AccessDenied",
    "AzureAccount",
    "AzureBlobBackend",
    "AzureBlobConfig",
    "ChecksumMismatch",
    "Unavailable",
    "from_azure_metadata",
    "normalize_posix",
    "posix_metadata",
    "to_azure_metadata",
]
