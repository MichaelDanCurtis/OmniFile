"""RFC 9457 problem details, as docs/contracts/control-api.md section 9 defines them.

Handlers raise `ApiError(code, detail)`; the app turns it into an
`application/problem+json` response. The HTTP status and the retry hint come
from the code, so a handler can't pair a code with the wrong status.
"""

from __future__ import annotations

import uuid
from typing import Any

from starlette.responses import JSONResponse

# code -> (HTTP status, title, retryable). Section 9 of the contract.
CODES: dict[str, tuple[int, str, bool]] = {
    "invalid_request": (400, "Invalid request", False),
    "certificate_required": (401, "Client certificate required", False),
    "certificate_invalid": (401, "Client certificate invalid", False),
    "enrolment_token_invalid": (401, "Enrolment token invalid", False),
    "edge_mismatch": (403, "Certificate does not match the edge", False),
    "edge_revoked": (403, "Edge revoked", False),
    "caller_not_allowed": (403, "Caller not allowed", False),
    "not_found": (404, "Not found", False),
    "version_conflict": (409, "Version conflict", False),
    "directory_not_empty": (409, "Directory not empty", False),
    "worm_violation": (409, "WORM violation", False),
    "stub_not_allowed": (409, "Stub not allowed", False),
    "request_in_progress": (409, "Request in progress", True),
    "policy_not_approved": (409, "Policy not approved", False),
    "layout_conflict": (409, "Layout conflict", False),
    "explicit_tier_set": (409, "Explicit tier set", False),
    "beyond_rewind_depth": (410, "Beyond rewind depth", False),
    "cursor_expired": (410, "Cursor expired", False),
    "idempotency_key_reused": (422, "Idempotency key reused", False),
    "acl_unknown": (422, "ACL unknown", False),
    "name_invalid": (422, "Name invalid", False),
    "dependency_failed": (422, "Dependency failed", False),
    "edge_upgrade_required": (426, "Edge upgrade required", False),
    "throttled": (429, "Throttled", True),
    "actor_required": (500, "Actor required", False),
    "internal": (500, "Internal error", True),
    "unavailable": (503, "Unavailable", True),
    # M4: portal API (portal-api.md §7) and Teams intents (teams.md §4.4).
    "unauthenticated": (401, "Unauthenticated", False),
    "reauth_required": (401, "Re-authentication required", False),
    "role_required": (403, "Role required", False),
    "forbidden": (403, "Forbidden", False),
    "csrf_failed": (403, "CSRF check failed", False),
    "self_approval": (403, "Self-approval not allowed", False),
    "intent_invalid": (403, "Intent invalid", False),
    "not_intended_recipient": (403, "Not the intended recipient", False),
    "intent_mismatch": (400, "Intent mismatch", False),
    "proposal_not_pending": (409, "Proposal not pending", False),
    "recall_not_needed": (409, "Recall not needed", False),
    "version_not_restorable": (409, "Version not restorable", False),
    "worm_window_closed": (409, "WORM window closed", False),
    "name_taken": (409, "Name taken", False),
    "intent_already_used": (409, "Intent already used", False),
    "intent_expired": (410, "Intent expired", False),
    "precondition_failed": (412, "Precondition failed", True),
    # M5: partner sources (portal-api.md §5.10).
    "source_conflict": (409, "Source conflict", False),
    "migration_conflict": (409, "Migration conflict", False),
    # M5: a partner's inline backend config (portal-api.md §5.10) and
    # recall batches (§5.19).
    "config_invalid": (422, "Config invalid", False),
    "batch_too_large": (422, "Batch too large", False),
    # M6: adding edges (portal-api.md §5.15).
    "edge_enrolled": (409, "Edge enrolled", False),
    # Decision 50: a Roles Anywhere CSR for a key other than the edge's pinned one.
    "aws_key_mismatch": (409, "AWS key mismatch", False),
    # M6: adding shares (portal-api.md §5.16).
    "dataset_shared": (409, "Dataset shared", False),
    "share_in_use": (409, "Share in use", False),
    # M6: API clients (portal-api.md §2.2, §5.21).
    "client_not_registered": (403, "Client not registered", False),
    "client_disabled": (403, "Client disabled", False),
    "owner_lacks_access": (403, "Owner lacks access", False),
    "confirmation_mismatch": (422, "Confirmation mismatch", False),
    "policy_invalid": (422, "Policy invalid", False),
    "action_not_allowed": (422, "Action not allowed", False),
    "precondition_required": (428, "Precondition required", False),
    "groups_pending": (503, "Groups pending", True),
    # Annexes (annex.md, phase A1).
    "dataset_annexed": (409, "Dataset annexed", False),
    "file_present": (409, "File present on the NAS", False),
    # A stop or acknowledgement of a copy no read-back refused (migration 0058).
    "copy_not_refused": (409, "Copy not refused", False),
    "mode_not_available": (422, "Mode not available", False),
    "owner_required": (422, "Owner required", False),
    # Annex rule sets (portal-api.md 5.23): the section 4.2 checks, in `rule_checks`.
    "rules_check_failed": (422, "Rule set check failed", False),
    # The return path (annex.md section 4.6, phase A3.1; portal-api.md 5.25).
    "target_not_allowed": (403, "Target not allowed", False),
    "requester_required": (422, "Requester required", False),
    "placement_not_cancellable": (409, "The placement can't be cancelled now", False),
    "placement_not_abandonable": (409, "The placement can't be abandoned now", False),
}

# Catalog trigger errors (SQLSTATE class OF, migration 0003) -> API codes.
SQLSTATE_CODES = {
    "OF001": "directory_not_empty",
    "OF002": "worm_violation",
    "OF003": "policy_not_approved",
    "OF004": "layout_conflict",
    "OF005": "actor_required",
    "OF006": "explicit_tier_set",
    "OF007": "version_conflict",
    # Migration 0035: approving an API client's proposal by someone answerable for it,
    # and a backend row the catalog's rules refuse.
    "OF010": "self_approval",
    "OF011": "invalid_request",
}

PROBLEM_JSON = "application/problem+json"


class ApiError(Exception):
    """A request failed with one of the documented codes."""

    def __init__(
        self,
        code: str,
        detail: str | None = None,
        *,
        retry_after_s: int | None = None,
        headers: dict[str, str] | None = None,
    ) -> None:
        if code not in CODES:
            raise ValueError(f"undocumented error code {code!r}")
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code = code
        self.detail = detail
        self.retry_after_s = retry_after_s
        self.headers = headers or {}

    @property
    def status(self) -> int:
        return CODES[self.code][0]

    def body(self, request_id: str | None = None) -> dict[str, Any]:
        status, title, retryable = CODES[self.code]
        out: dict[str, Any] = {
            "type": f"urn:omnifile:error:{self.code}",
            "title": title,
            "status": status,
            "code": self.code,
            "retryable": retryable,
            "retry_after_s": self.retry_after_s,
            "request_id": request_id or uuid.uuid4().hex,
        }
        if self.detail:
            out["detail"] = self.detail
        return out

    def response(self, request_id: str | None = None) -> JSONResponse:
        headers = dict(self.headers)
        if self.retry_after_s is not None:
            headers["Retry-After"] = str(self.retry_after_s)
        return JSONResponse(
            self.body(request_id), status_code=self.status, headers=headers, media_type=PROBLEM_JSON
        )


def from_sqlstate(sqlstate: str | None, message: str) -> ApiError | None:
    """The API error for a catalog trigger error, or None if it is not one."""
    code = SQLSTATE_CODES.get(sqlstate or "")
    return ApiError(code, message) if code else None
