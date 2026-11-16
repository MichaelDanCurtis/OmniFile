"""Problem details for `web` (portal-api.md section 7).

The codes and statuses are `api.problems.CODES`; `WebError` adds the
extension members the portal uses: `errors` (invalid_request), `checks`
(policy_invalid) and `max_age_s` (reauth_required).
"""

from __future__ import annotations

from typing import Any

from ..api.problems import ApiError


class WebError(ApiError):
    def __init__(
        self,
        code: str,
        detail: str | None = None,
        *,
        retry_after_s: int | None = None,
        headers: dict[str, str] | None = None,
        **extensions: Any,
    ) -> None:
        super().__init__(code, detail, retry_after_s=retry_after_s, headers=headers)
        self.extensions = {k: v for k, v in extensions.items() if v is not None}

    def body(self, request_id: str | None = None) -> dict[str, Any]:
        out = super().body(request_id)
        out.update(self.extensions)
        return out


def invalid(field: str, message: str) -> WebError:
    """`400 invalid_request` naming one field."""
    return WebError(
        "invalid_request", f"{field}: {message}", errors=[{"field": field, "message": message}]
    )


def not_found(what: str = "item") -> WebError:
    """Unreadable and missing look the same (decision 28)."""
    return WebError("not_found", f"no such {what}")


def role_required(*roles: str) -> WebError:
    return WebError("role_required", "needs the app role " + " or ".join(roles))


def forbidden(detail: str) -> WebError:
    return WebError("forbidden", detail)
