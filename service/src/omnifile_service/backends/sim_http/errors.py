"""Azure Storage error responses.

Format and codes from Microsoft's "Status and error codes", "Common REST API
error codes" and "Blob Storage error codes" pages, cross-checked against the
`StorageErrorCode` enum in azure-rest-api-specs
(specification/storage/data-plane/Microsoft.BlobStorage/stable/2026-12-06/blob.json).
"""

from __future__ import annotations

from collections.abc import Mapping
from xml.sax.saxutils import escape


class AzureError(Exception):
    """An error the facade returns as an Azure-style XML error response."""

    def __init__(
        self,
        status: int,
        code: str,
        message: str,
        *,
        auth_detail: str | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> None:
        super().__init__(f"{status} {code}: {message}")
        self.status = status
        self.code = code
        self.message = message
        self.auth_detail = auth_detail
        self.headers = dict(headers or {})


def error_body(err: AzureError, request_id: str, time_iso: str) -> bytes:
    # Azure appends the request id and time to <Message>.
    msg = f"{err.message}\nRequestId:{request_id}\nTime:{time_iso}"
    detail = (
        f"<AuthenticationErrorDetail>{escape(err.auth_detail)}</AuthenticationErrorDetail>"
        if err.auth_detail
        else ""
    )
    return (
        '<?xml version="1.0" encoding="utf-8"?>'
        f"<Error><Code>{escape(err.code)}</Code><Message>{escape(msg)}</Message>{detail}</Error>"
    ).encode()


# --- the errors the facade uses (status, code, message as Microsoft words them) ---


def authentication_failed(detail: str) -> AzureError:
    return AzureError(
        403,
        "AuthenticationFailed",
        "Server failed to authenticate the request. Make sure the value of Authorization "
        "header is formed correctly including the signature.",
        auth_detail=detail,
    )


def no_authentication() -> AzureError:
    return AzureError(
        401,
        "NoAuthenticationInformation",
        "Server failed to authenticate the request. Please refer to the information in the "
        "www-authenticate header.",
    )


def permission_mismatch() -> AzureError:
    return AzureError(
        403,
        "AuthorizationPermissionMismatch",
        "This request is not authorized to perform this operation using this permission.",
    )


def protocol_mismatch() -> AzureError:
    return AzureError(
        403,
        "AuthorizationProtocolMismatch",
        "This request is not authorized to perform this operation using this protocol.",
    )


def source_ip_mismatch() -> AzureError:
    return AzureError(
        403,
        "AuthorizationSourceIPMismatch",
        "This request is not authorized to perform this operation using this source IP.",
    )


def blob_not_found() -> AzureError:
    return AzureError(404, "BlobNotFound", "The specified blob does not exist.")


def blob_archived() -> AzureError:
    return AzureError(409, "BlobArchived", "This operation is not permitted on an archived blob.")


def blob_being_rehydrated() -> AzureError:
    return AzureError(
        409,
        "BlobBeingRehydrated",
        "This operation is not permitted because the blob is being rehydrated.",
    )


def md5_mismatch() -> AzureError:
    return AzureError(
        400,
        "Md5Mismatch",
        "The MD5 value specified in the request did not match the MD5 value calculated by the "
        "server.",
    )


#: Put Block documents only "fails with error code 400 (Bad Request)" for a CRC64
#: mismatch, and the REST spec's StorageErrorCode enum has no CRC64 member.
#: The code string below is UNVERIFIED: check it in the real-Azure contract lane.
CRC64_MISMATCH_CODE = "Crc64Mismatch"


def crc64_mismatch() -> AzureError:
    return AzureError(
        400,
        CRC64_MISMATCH_CODE,
        "The CRC64 value specified in the request did not match the CRC64 value calculated by "
        "the server.",
    )


def invalid_header(name: str, why: str = "") -> AzureError:
    return AzureError(
        400,
        "InvalidHeaderValue",
        "The value for one of the HTTP headers is not in the correct format."
        + (f" {name}: {why}" if why else f" {name}"),
    )


def invalid_md5() -> AzureError:
    return AzureError(
        400,
        "InvalidMd5",
        "The MD5 value specified in the request is invalid. The MD5 value must be 128 bits and "
        "Base64-encoded.",
    )


def not_implemented(what: str) -> AzureError:
    """Anything outside the subset the edge uses. Deliberately not an Azure code."""
    return AzureError(501, "NotImplemented", f"sim_http does not implement {what}.")
