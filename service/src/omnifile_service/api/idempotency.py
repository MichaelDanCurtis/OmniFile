"""Idempotency-Key handling (control-api.md section 8), stored in catalog.idempotency_key.

- Same key, same body: the stored response is returned with
  `Idempotent-Replayed: true`.
- Same key, different body: `422 idempotency_key_reused`.
- Same key while the first request still runs: `409 request_in_progress`.

The claim, the work and the stored result are three short transactions, so a
request never holds a catalog transaction open while it waits on anything.
Retryable failures (5xx, `request_in_progress`, throttling) release the claim,
so a retry with the same key runs again; other errors are stored and replayed
like successes. A claim left behind by a crashed replica is taken over after
`STALE_CLAIM`.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from psycopg.types.json import Jsonb
from starlette.responses import JSONResponse, Response

from .problems import CODES, PROBLEM_JSON, ApiError

STALE_CLAIM_SECONDS = 300
KEY_HEADER = "idempotency-key"


@dataclass(frozen=True)
class Result:
    status: int
    body: dict[str, Any] | None = None

    def response(self, replayed: bool = False) -> Response:
        headers = {"Idempotent-Replayed": "true"} if replayed else {}
        if self.body is None:
            return Response(status_code=self.status, headers=headers)
        media = PROBLEM_JSON if self.status >= 400 else "application/json"
        return JSONResponse(self.body, status_code=self.status, headers=headers, media_type=media)


def request_key(headers) -> str:
    key = headers.get(KEY_HEADER)
    if key is None:
        raise ApiError("invalid_request", "Idempotency-Key header is required")
    if not 16 <= len(key) <= 128 or not key.isascii() or not key.isprintable():
        raise ApiError("invalid_request", "Idempotency-Key must be 16-128 printable characters")
    return key


def request_hash(method: str, path: str, raw: bytes) -> bytes:
    return hashlib.sha256(method.encode() + b"\n" + path.encode() + b"\n" + raw).digest()


async def run_idempotent(
    catalog,
    principal: str,
    key: str,
    method: str,
    path: str,
    raw: bytes,
    handler: Callable[[], Awaitable[Result]],
    *,
    request_id: str,
) -> Response:
    digest = request_hash(method, path, raw)
    stored = await _claim(catalog, principal, key, method, path, digest)
    if stored is not None:
        return stored.response(replayed=True)
    try:
        result = await handler()
    except ApiError as exc:
        _, _, retryable = CODES[exc.code]
        if retryable or exc.status >= 500:
            await _release(catalog, principal, key)
        else:
            await _store(catalog, principal, key, Result(exc.status, exc.body(request_id)))
        raise
    except BaseException:
        await _release(catalog, principal, key)
        raise
    await _store(catalog, principal, key, result)
    return result.response()


async def _claim(
    catalog, principal: str, key: str, method: str, path: str, digest: bytes
) -> Result | None:
    async with catalog.tx(principal) as db:
        await db.execute(
            "DELETE FROM catalog.idempotency_key"
            " WHERE principal = %s AND key = %s AND expires_at < now()",
            (principal, key),
        )
        inserted = await (
            await db.execute(
                "INSERT INTO catalog.idempotency_key"
                " (principal, key, method, path, request_sha256)"
                " VALUES (%s, %s, %s, %s, %s)"
                " ON CONFLICT (principal, key) DO NOTHING RETURNING key",
                (principal, key, method, path, digest),
            )
        ).fetchone()
        if inserted is not None:
            return None
        row = await (
            await db.execute(
                "SELECT request_sha256, status, response,"
                "       created_at < now() - make_interval(secs => %s) AS stale"
                "  FROM catalog.idempotency_key WHERE principal = %s AND key = %s FOR UPDATE",
                (STALE_CLAIM_SECONDS, principal, key),
            )
        ).fetchone()
        if row is None:  # released between our insert and select; let the client retry
            raise ApiError("request_in_progress", "retry", retry_after_s=1)
        if bytes(row["request_sha256"]) != digest:
            raise ApiError("idempotency_key_reused", "this key was used with a different request")
        if row["status"] is None:
            if not row["stale"]:
                raise ApiError(
                    "request_in_progress", "the first request is still running", retry_after_s=1
                )
            await db.execute(
                "UPDATE catalog.idempotency_key SET created_at = now()"
                " WHERE principal = %s AND key = %s",
                (principal, key),
            )
            return None
        body = row["response"]
        if isinstance(body, str):
            body = json.loads(body)
        return Result(row["status"], body)


async def _store(catalog, principal: str, key: str, result: Result) -> None:
    async with catalog.tx(principal) as db:
        await db.execute(
            "UPDATE catalog.idempotency_key SET status = %s, response = %s"
            " WHERE principal = %s AND key = %s",
            (result.status, None if result.body is None else Jsonb(result.body), principal, key),
        )


async def _release(catalog, principal: str, key: str) -> None:
    try:
        async with catalog.tx(principal) as db:
            await db.execute(
                "DELETE FROM catalog.idempotency_key"
                " WHERE principal = %s AND key = %s AND status IS NULL",
                (principal, key),
            )
    except ApiError:
        pass  # the claim goes stale and is taken over later
