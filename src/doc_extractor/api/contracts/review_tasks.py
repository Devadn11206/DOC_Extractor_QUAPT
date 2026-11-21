"""
Review-task API contracts (Module M5 / Phase 3, Slice 1: API-005 list, API-016 claim and release), PROVISIONAL.

Defines the provisional scopes, the path-safe task identifier rule, the public response DTOs and the validation of the list
query. Nothing here is owner-approved: the task states, the scope names, the error codes and the owner exposure all depend on
the open decision M5-D11 (see ``docs/m5-open-decisions.md``, "Phase 3 Slice 1").

Privacy (SEC-012): the public task carries identifiers, the task state, the owner's principal ID and timestamps ONLY. The
record's ``reviewer_notes``, ``metadata`` and ``priority`` are never exposed, and neither is ``tenant_id``. No extracted value,
no old or new field value and no correction text exists in this slice.
"""

import re
from datetime import datetime
from typing import Any, Iterable, List, Optional

from pydantic import BaseModel, ConfigDict, Field

from doc_extractor.api.contracts.documents import SAFE_DOCUMENT_ID_REGEX, PagingInfo
from doc_extractor.api.contracts.records import ReviewTaskRecord, ReviewTaskStatus
from doc_extractor.api.exceptions import ApiValidationError, ReviewTaskNotFoundError

# PROVISIONAL scopes (not owner-approved: the scope catalogue and the role-by-endpoint matrix, SEC-003, are open).
SCOPE_REVIEW_TASKS_READ: str = "review_tasks:read"
SCOPE_REVIEW_TASKS_CLAIM: str = "review_tasks:claim"      # claim AND release

# Same path-safe rule as document identifiers: letters, digits, hyphen and underscore, 1 to 128 characters.
SAFE_TASK_ID_REGEX = SAFE_DOCUMENT_ID_REGEX

DEFAULT_PAGE_SIZE = 10
MAX_PAGE_SIZE = 100            # provisional: the PRD names no page size for the review queue

LIST_QUERY_PARAMETERS = frozenset({"page", "size", "status", "assignee", "document_id", "documentType", "flagType", "slaAge"})
UNSUPPORTED_LIST_FILTERS = ("documentType", "flagType", "slaAge")

_UNKNOWN_PARAMETER_MESSAGE = (
    "Unknown query parameter. Allowed parameters: page, size, status, assignee, document_id, documentType, flagType, slaAge."
)


def validate_task_id(task_id: Any) -> str:
    """
    A path-safe task identifier. A malformed one is the concealed 404 ``REVIEW_TASK_NOT_FOUND``, exactly like an unknown or
    another tenant's task, so the identifier format cannot be probed (SEC-006). Nothing the client sent is echoed.
    """
    if not isinstance(task_id, str) or isinstance(task_id, bool) or not SAFE_TASK_ID_REGEX.fullmatch(task_id):
        raise ReviewTaskNotFoundError()
    return task_id


def _validation_error(location: List[str], message: str, error_type: str) -> ApiValidationError:
    """A 422 in the framework's own ``details`` shape; the offending value is never echoed."""
    return ApiValidationError(
        message="Request validation failed. Check parameter and body specifications.",
        details={"validation_errors": [{"location": location, "message": message, "type": error_type}]},
    )


def reject_unknown_list_parameters(names: Iterable[str]) -> None:
    """A misspelled or invented query name is a 422, never a silently ignored one. No offending name is reflected."""
    if any(name not in LIST_QUERY_PARAMETERS for name in names):
        raise _validation_error(["query"], _UNKNOWN_PARAMETER_MESSAGE, "unknown_parameter")


def reject_unsupported_list_filter(parameter: str, value: Optional[str]) -> None:
    """``documentType``, ``flagType`` and ``slaAge`` are PRD toolbar filters whose data is not modelled yet: a 422, not ignored."""
    if value is not None:
        raise _validation_error(
            ["query", parameter], "This filter is not supported: the data it needs is not stored yet.", "unsupported_filter"
        )


def validate_document_id_filter(value: Optional[str]) -> Optional[str]:
    """The ``document_id`` filter: ``None`` or a path-safe identifier; anything else is a 422 that does not echo it."""
    if value is None:
        return None
    if not SAFE_DOCUMENT_ID_REGEX.fullmatch(value):
        raise _validation_error(["query", "document_id"], "Invalid document identifier.", "invalid_identifier")
    return value


def reject_unexpected_action_input(query_names: Iterable[str], has_body: bool) -> None:
    """
    Claim and release take NO input: the owner is always the authenticated principal. Any query parameter or body (for
    example a ``reviewer_id`` trying to claim for someone else) is a 422, never silently ignored. Nothing is echoed.
    """
    if any(True for _ in query_names):
        raise _validation_error(["query"], "This operation takes no query parameters.", "unexpected_parameter")
    if has_body:
        raise _validation_error(["body"], "This operation takes no request body.", "unexpected_body")


class ReviewTaskResource(BaseModel):
    """
    One review task as exposed to a caller (API-005 / API-016). PROVISIONAL: ``assigned_reviewer_id`` is the owner's
    principal ID, no display name exists (M5-D11, M2D-D02: whether principal IDs may be shown to other reviewers is open).
    ``assigned_to_me`` is derived from the authenticated principal. PRD fields not modelled yet (flagged fields, flag reasons,
    SLA due time, document type) are absent, not faked.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    task_id: str = Field(..., min_length=1)
    document_id: str = Field(..., min_length=1)
    status: ReviewTaskStatus = Field(..., description="Provisional: unassigned, claimed or completed (M5-D11)")
    assigned_reviewer_id: Optional[str] = Field(None, description="Principal ID of the owner; null when unassigned")
    assigned_to_me: bool = Field(..., description="True when the authenticated principal owns the task")
    created_at: datetime
    updated_at: datetime
    completed_at: Optional[datetime] = None

    @classmethod
    def from_record(cls, record: ReviewTaskRecord, principal_id: str) -> "ReviewTaskResource":
        return cls(
            task_id=record.task_id,
            document_id=record.document_id,
            status=record.status,
            assigned_reviewer_id=record.assigned_reviewer_id,
            assigned_to_me=record.assigned_reviewer_id is not None and record.assigned_reviewer_id == principal_id,
            created_at=record.created_at,
            updated_at=record.updated_at,
            completed_at=record.completed_at,
        )


class ReviewTaskListResponse(BaseModel):
    """API-005 list response: one page, oldest task first, with the paging block shared with API-015."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    content: List[ReviewTaskResource]
    paging: PagingInfo
