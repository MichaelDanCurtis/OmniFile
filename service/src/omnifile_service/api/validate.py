"""Request parsing: the field rules of service/openapi/control-api.yaml, by hand.

Every helper raises `ApiError("invalid_request", ...)` naming the field, so a
schema violation is always a 400 with a useful detail. The spec test
(tests/api/test_openapi_conformance.py) checks that what the handlers accept
and return agrees with the spec.
"""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from typing import Any

from starlette.requests import Request

from .problems import ApiError

MAX_BODY = 4 * 1024 * 1024  # control-api.md section 10

_ID = re.compile(r"^[0-9]{1,19}$")
_NANOS = re.compile(r"^-?[0-9]{1,20}$")
_SID = re.compile(r"^S-1-[0-9]+(-[0-9]+){1,15}$")
_HEX64 = re.compile(r"^[0-9a-f]{64}$")
_HEX16 = re.compile(r"^[0-9a-f]{16}$")
_CURSOR = re.compile(r"^[0-9]+\.[0-9]+(\.[0-9]+)?$")
MAX_ID = 2**63 - 1


def bad(detail: str) -> ApiError:
    return ApiError("invalid_request", detail)


async def json_body(request: Request, limit: int = MAX_BODY) -> tuple[bytes, dict[str, Any]]:
    """The raw body (for idempotency hashing) and its JSON object."""
    declared = request.headers.get("content-length")
    if declared is not None and (not declared.isdigit() or int(declared) > limit):
        raise bad(f"body larger than {limit} bytes")
    chunks: list[bytes] = []
    size = 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > limit:
            raise bad(f"body larger than {limit} bytes")
        chunks.append(chunk)
    raw = b"".join(chunks)
    if not raw:
        return raw, {}
    try:
        body = json.loads(raw)
    except (ValueError, RecursionError):
        raise bad("body is not JSON") from None
    if not isinstance(body, dict):
        raise bad("body must be a JSON object")
    return raw, body


def parse_id(value: Any, field: str) -> int:
    if not isinstance(value, str) or not _ID.match(value) or int(value) > MAX_ID:
        raise bad(f"{field} must be a 64-bit id as a decimal string")
    return int(value)


def req(body: dict[str, Any], field: str) -> Any:
    if field not in body:
        raise bad(f"{field} is required")
    return body[field]


def req_id(body: dict[str, Any], field: str) -> int:
    return parse_id(req(body, field), field)


def opt_id(body: dict[str, Any], field: str) -> int | None:
    value = body.get(field)
    return None if value is None else parse_id(value, field)


def req_str(body: dict[str, Any], field: str, *, max_len: int | None = None) -> str:
    value = req(body, field)
    if not isinstance(value, str):
        raise bad(f"{field} must be a string")
    if max_len is not None and len(value) > max_len:
        raise bad(f"{field} is longer than {max_len}")
    return value


def opt_str(body: dict[str, Any], field: str) -> str | None:
    value = body.get(field)
    if value is not None and not isinstance(value, str):
        raise bad(f"{field} must be a string")
    return value


def req_int(body: dict[str, Any], field: str, *, minimum: int | None = None) -> int:
    return _int(req(body, field), field, minimum)


def opt_int(body: dict[str, Any], field: str, *, minimum: int | None = None) -> int | None:
    value = body.get(field)
    return None if value is None else _int(value, field, minimum)


def _int(value: Any, field: str, minimum: int | None) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise bad(f"{field} must be an integer")
    if minimum is not None and value < minimum:
        raise bad(f"{field} must be >= {minimum}")
    if not -(2**63) <= value <= MAX_ID:
        raise bad(f"{field} is out of range")
    return value


def req_bool(body: dict[str, Any], field: str, default: bool | None = None) -> bool:
    if field not in body and default is not None:
        return default
    value = req(body, field)
    if not isinstance(value, bool):
        raise bad(f"{field} must be a boolean")
    return value


def req_enum(body: dict[str, Any], field: str, allowed: tuple[str, ...]) -> str:
    value = req(body, field)
    if value not in allowed:
        raise bad(f"{field} must be one of {', '.join(allowed)}")
    return value


def nanos(value: Any, field: str) -> int:
    if not isinstance(value, str) or not _NANOS.match(value):
        raise bad(f"{field} must be nanoseconds as a decimal string")
    n = int(value)
    if not -(2**63) <= n <= MAX_ID:
        raise bad(f"{field} is out of range")
    return n


def hex_bytes(value: Any, field: str, size: int) -> bytes:
    pattern = _HEX64 if size == 32 else _HEX16
    if not isinstance(value, str) or not pattern.match(value):
        raise bad(f"{field} must be {size * 2} lowercase hex digits")
    return bytes.fromhex(value)


def sid(value: Any, field: str) -> str:
    if not isinstance(value, str) or not _SID.match(value):
        raise bad(f"{field} must be a SID")
    return value


def timestamp(value: Any, field: str) -> datetime:
    if not isinstance(value, str):
        raise bad(f"{field} must be an RFC 3339 time")
    try:
        t = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise bad(f"{field} must be an RFC 3339 time") from None
    if t.tzinfo is None:
        raise bad(f"{field} must carry a time zone")
    return t.astimezone(UTC)


def cursor(value: str | None, field: str = "after") -> tuple[int, int]:
    if value is None or not _CURSOR.match(value):
        raise bad(f"{field} must be a feed cursor such as 0.0")
    a, b = value.split(".")[:2]
    ta, tb = int(a), int(b)
    if ta > MAX_ID or tb > MAX_ID:
        raise bad(f"{field} is out of range")
    return ta, tb


def cursor_incarnation(value: str) -> int:
    """The feed incarnation a well-formed cursor was issued in (migration
    0019): its third part, 0 when it has none."""
    parts = value.split(".")
    return int(parts[2]) if len(parts) > 2 else 0


def query_int(request: Request, name: str, default: int, lo: int, hi: int) -> int:
    raw = request.query_params.get(name)
    if raw is None:
        return default
    if not raw.isdigit() or not lo <= int(raw) <= hi:
        raise bad(f"{name} must be an integer from {lo} to {hi}")
    return int(raw)


def rfc3339(t: datetime | None) -> str | None:
    if t is None:
        return None
    return t.astimezone(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")
