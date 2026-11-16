"""An Azure Blob data-plane HTTP facade over the simulated backend (sim).

Lets the Rust edge's real uploader and downloader run end to end on a laptop
without Azure. See README.md in this directory.
"""

from .app import BlobFacade, azure_crc64, create_app
from .errors import CRC64_MISMATCH_CODE, AzureError
from .sas import UserDelegationKey, mint

__all__ = [
    "CRC64_MISMATCH_CODE",
    "AzureError",
    "BlobFacade",
    "UserDelegationKey",
    "azure_crc64",
    "create_app",
    "mint",
]
