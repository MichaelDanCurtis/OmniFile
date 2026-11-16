"""Map Azure SDK errors to the backend contract's errors.

Codes are from Microsoft's Blob Storage error-code table
(https://learn.microsoft.com/en-us/rest/api/storageservices/blob-service-error-codes,
read 2026-10-05). Azure documents a CRC64 mismatch on Put Block only as
"400 (Bad Request)"; the code string ``Crc64Mismatch`` used below is the one
sim_http answers with and is UNVERIFIED against real Azure (check it in the
real-Azure contract lane). ``Md5Mismatch`` is documented.
"""

from __future__ import annotations

from datetime import timedelta

from azure.core.exceptions import (
    AzureError,
    ClientAuthenticationError,
    HttpResponseError,
    ResourceNotFoundError,
    ServiceRequestError,
    ServiceResponseError,
)

from ..base import (
    BackendError,
    Immutable,
    NotRestored,
    ObjectNotFound,
    Throttled,
    UnsupportedOperation,
)


class ChecksumMismatch(BackendError):
    """The service rejected a block, or the caller's whole-object CRC differs."""


class Unavailable(BackendError):
    """Azure could not be reached or answered 5xx; retry later."""


class AccessDenied(BackendError):
    """Entra / RBAC refused the call (missing role assignment, bad token)."""


IMMUTABLE_CODES = frozenset(
    {
        "BlobImmutableDueToPolicy",
        "BlobImmutableDueToLegalHold",
        "ContainerImmutabilityPolicyLocked",
        "ContainerHasLegalHold",
    }
)
NOT_RESTORED_CODES = frozenset({"BlobArchived", "BlobBeingRehydrated"})
UNSUPPORTED_CODES = frozenset(
    {
        "InvalidBlobTier",
        "BlobAccessTierNotSupportedForAccountType",
        "CannotChangeToLowerTier",
        "CopyAcrossAccountsNotSupported",
        "BlobNotArchived",
        "FeatureNotSupported",
        "OperationNotSupportedOnHierarchicalNamespace",
    }
)
CHECKSUM_CODES = frozenset({"Md5Mismatch", "Crc64Mismatch", "InvalidBlobOrBlock"})
THROTTLE_CODES = frozenset({"ServerBusy", "OperationTimedOut", "TooManyRequests"})
NOT_FOUND_CODES = frozenset({"BlobNotFound", "ContainerNotFound", "ResourceNotFound"})


def error_code(exc: BaseException) -> str | None:
    code = getattr(exc, "error_code", None)
    if code is None:
        response = getattr(exc, "response", None)
        headers = getattr(response, "headers", None)
        if headers is not None:
            code = headers.get("x-ms-error-code")
    if code is None:
        return None
    return str(getattr(code, "value", code))


def _retry_after(exc: BaseException) -> timedelta | None:
    response = getattr(exc, "response", None)
    headers = getattr(response, "headers", None) or {}
    raw = headers.get("Retry-After") if hasattr(headers, "get") else None
    try:
        return timedelta(seconds=float(raw)) if raw is not None else None
    except ValueError:
        return None


def translate(exc: BaseException, what: str, *, archived_means: type[BackendError] = NotRestored):
    """Return the contract error for an SDK exception.

    `archived_means` picks what ``BlobArchived`` becomes: `NotRestored` for
    reads and copies, `UnsupportedOperation` for metadata changes [A].
    """
    if isinstance(exc, BackendError):
        return exc
    code = error_code(exc)
    status = getattr(exc, "status_code", None)
    msg = f"{what}: {code or type(exc).__name__}: {str(exc).splitlines()[0] if str(exc) else ''}"
    if code in IMMUTABLE_CODES:
        return Immutable(msg)
    if code in NOT_RESTORED_CODES:
        return archived_means(msg)
    if code in UNSUPPORTED_CODES:
        return UnsupportedOperation(msg)
    if code in CHECKSUM_CODES:
        return ChecksumMismatch(msg)
    if code in THROTTLE_CODES or status in (429, 503):
        return Throttled(msg, _retry_after(exc))
    if isinstance(exc, ResourceNotFoundError) or code in NOT_FOUND_CODES or status == 404:
        return ObjectNotFound(msg)
    if isinstance(exc, ClientAuthenticationError) or status in (401, 403):
        return AccessDenied(msg)
    if isinstance(exc, ServiceRequestError | ServiceResponseError):
        return Unavailable(msg)
    if isinstance(exc, HttpResponseError) and status is not None and status >= 500:
        return Unavailable(msg)
    if isinstance(exc, AzureError):
        return BackendError(msg)
    return BackendError(msg)
