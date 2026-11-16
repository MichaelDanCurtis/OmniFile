"""S3 and SQS errors, mapped onto the backend contract's errors."""

from __future__ import annotations

from datetime import timedelta

from botocore.exceptions import BotoCoreError, ClientError

from ..base import (
    BackendError,
    NotRestored,
    ObjectNotFound,
    Throttled,
    UnsupportedOperation,
)

NOT_FOUND = frozenset({"NoSuchKey", "NoSuchVersion", "NoSuchBucket", "NotFound", "404"})
#: S3 SlowDown/503 and the throttling family; restores above the account's
#: 1,000/s answer ThrottlingException [V].
THROTTLED = frozenset(
    {
        "SlowDown",
        "Throttling",
        "ThrottlingException",
        "RequestLimitExceeded",
        "TooManyRequestsException",
        "ServiceUnavailable",
        "503",
    }
)


def code_of(e: ClientError) -> str:
    return str(e.response.get("Error", {}).get("Code") or "")


def status_of(e: ClientError) -> int:
    return int(e.response.get("ResponseMetadata", {}).get("HTTPStatusCode") or 0)


def retry_after(e: ClientError) -> timedelta | None:
    raw = e.response.get("ResponseMetadata", {}).get("HTTPHeaders", {}).get("retry-after")
    try:
        return timedelta(seconds=float(raw)) if raw else None
    except ValueError:
        return None


def translate(e: Exception, what: str, *, reading: bool = False) -> BackendError:
    """A contract error for `e`. `reading`: a GET, where InvalidObjectState
    means the object is archived without a live restored copy."""
    if isinstance(e, BackendError):
        return e
    if isinstance(e, ClientError):
        code, status = code_of(e), status_of(e)
        if code in NOT_FOUND or status == 404:
            return ObjectNotFound(f"{what}: {code or status}")
        if code in THROTTLED or status == 503:
            return Throttled(f"{what}: {code or status}", retry_after=retry_after(e))
        if code == "InvalidObjectState":
            if reading:
                return NotRestored(f"{what} is archived and not restored")
            return UnsupportedOperation(f"{what}: archived; restore it first (InvalidObjectState)")
        if code == "GlacierExpeditedRetrievalNotAvailable":
            # Expedited capacity is not guaranteed under high demand [V].
            return Throttled(f"{what}: no Expedited capacity now")
        if code in ("InvalidStorageClass", "NotImplemented"):
            return UnsupportedOperation(f"{what}: {code}")
        return BackendError(f"{what}: {code or status} {e}")
    if isinstance(e, BotoCoreError):
        return BackendError(f"{what}: {e}")
    return BackendError(f"{what}: {e!r}")
