"""
Review Tasks REST API Router (Module M5 / Phase 3, Slice 1), PROVISIONAL.

Endpoints:
- GET /review-tasks (API-005, list: tenant-scoped, filtered, oldest first, paged)
- POST /review-tasks/{task_id}/claim (API-016, atomic at the repository port; idempotent for the owner)
- POST /review-tasks/{task_id}/release (API-016, only the current owner may release)

NOT here (a later slice): POST /review-tasks/{id}/complete, corrections and their history, review-task creation from
extraction, durable audit, four-eyes approval. There is no production persistence adapter: without an injected
``ReviewTaskRepository`` every route answers 503 after authentication and the scope check.
"""

from typing import Optional

from fastapi import APIRouter, Depends, Path, Query, Request, Response

from doc_extractor.api.contracts.documents import PagingInfo
from doc_extractor.api.contracts.errors import ErrorEnvelope
from doc_extractor.api.contracts.records import ReviewTaskStatus
from doc_extractor.api.contracts.review_tasks import (
    DEFAULT_PAGE_SIZE,
    MAX_PAGE_SIZE,
    SCOPE_REVIEW_TASKS_CLAIM,
    SCOPE_REVIEW_TASKS_READ,
    ReviewTaskListResponse,
    ReviewTaskResource,
    reject_unexpected_action_input,
    reject_unknown_list_parameters,
    reject_unsupported_list_filter,
    validate_document_id_filter,
)
from doc_extractor.api.dependencies import get_current_principal, get_review_task_service, require_scopes
from doc_extractor.api.ports.authentication import ApiPrincipal
from doc_extractor.api.ports.repositories import ReviewTaskAssignee
from doc_extractor.api.services.review_task_service import ReviewTaskService

router = APIRouter(tags=["Review Tasks"])

_NO_STORE = "private, no-store"


async def _reject_unexpected_action_input(request: Request) -> None:
    """
    Claim and release take no input (the owner is always the authenticated principal). Query parameters and a request body are
    refused with 422, never silently ignored. The body is detected from the framing headers only: nothing is read, so an
    oversized or slow body costs nothing here. A dependency, so it runs after authentication and the scope check.
    """
    headers = request.headers
    has_body = headers.get("transfer-encoding") is not None or headers.get("content-length", "0").strip() not in ("", "0")
    reject_unexpected_action_input(request.query_params.keys(), has_body)


@router.get(
    "/review-tasks",
    dependencies=[Depends(require_scopes(SCOPE_REVIEW_TASKS_READ))],
    response_model=ReviewTaskListResponse,
    summary="List review tasks",
    description=(
        "Lists the review tasks of the caller's tenant, OLDEST FIRST (`created_at` ascending, then `task_id`), with `paging`. "
        "PROVISIONAL (M5-D11 is Open): states are `unassigned`, `claimed` and `completed`; the scope `review_tasks:read` is "
        "not owner-approved. Filters: `status`, `assignee` (`mine` = the authenticated principal, `unassigned`, `all`; "
        "default `all`) and `document_id` (exact). With no `status`, tasks in every state are listed. `documentType`, "
        "`flagType` and `slaAge` are not supported yet and are rejected with 422, as is any unknown query parameter. A task "
        "carries identifiers, state, the owner's principal ID (no display name exists) and timestamps only: no flagged "
        "fields, flag reasons or SLA yet. No snapshot is kept between requests. Without a configured repository the answer is 503."
    ),
    responses={
        401: {"model": ErrorEnvelope, "description": "Authentication required"},
        403: {"model": ErrorEnvelope, "description": "Insufficient scope (requires review_tasks:read)"},
        422: {"model": ErrorEnvelope, "description": "Invalid, unknown or unsupported query parameter"},
        500: {"model": ErrorEnvelope, "description": "The repository failed or returned an invalid result"},
        503: {"model": ErrorEnvelope, "description": "No ReviewTaskRepository is configured (no production adapter yet)"},
    },
)
async def list_review_tasks(
    request: Request,
    response: Response,
    page: int = Query(1, ge=1, description="1-based page number"),
    size: int = Query(DEFAULT_PAGE_SIZE, ge=1, le=MAX_PAGE_SIZE, description="Page size (provisional maximum 100)"),
    status: Optional[ReviewTaskStatus] = Query(None, description="Task state (provisional: M5-D11)"),
    assignee: ReviewTaskAssignee = Query(ReviewTaskAssignee.ALL, description="mine, unassigned or all"),
    document_id: Optional[str] = Query(None, max_length=128, description="Exact document identifier"),
    document_type: Optional[str] = Query(None, alias="documentType", description="NOT SUPPORTED: rejected with 422"),
    flag_type: Optional[str] = Query(None, alias="flagType", description="NOT SUPPORTED: rejected with 422"),
    sla_age: Optional[str] = Query(None, alias="slaAge", description="NOT SUPPORTED: rejected with 422"),
    principal: ApiPrincipal = Depends(get_current_principal),
    service: ReviewTaskService = Depends(get_review_task_service),
) -> ReviewTaskListResponse:
    """Lists the caller's tenant's review tasks (API-005). The tenant and the reviewer come only from the principal."""
    reject_unknown_list_parameters(request.query_params.keys())
    reject_unsupported_list_filter("documentType", document_type)
    reject_unsupported_list_filter("flagType", flag_type)
    reject_unsupported_list_filter("slaAge", sla_age)
    result = await service.list_tasks(
        status=status,
        assignee=assignee,
        document_id=validate_document_id_filter(document_id),
        page=page,
        size=size,
        request_id=getattr(request.state, "request_id", None),
    )
    total_pages = -(-result.total_items // size)
    body = ReviewTaskListResponse(
        content=[ReviewTaskResource.from_record(task, principal.principal_id) for task in result.items],
        paging=PagingInfo(page=page, size=size, total_items=result.total_items, total_pages=total_pages),
    )
    response.headers["Cache-Control"] = _NO_STORE
    return body


_ACTION_RESPONSES = {
    401: {"model": ErrorEnvelope, "description": "Authentication required"},
    403: {"model": ErrorEnvelope, "description": "Insufficient scope (requires review_tasks:claim)"},
    404: {"model": ErrorEnvelope, "description": "REVIEW_TASK_NOT_FOUND: unknown, malformed or another tenant's task"},
    409: {"model": ErrorEnvelope, "description": "TASK_CLAIMED (owned by another reviewer) or TASK_COMPLETED"},
    422: {"model": ErrorEnvelope, "description": "A query parameter or body was sent: this operation takes no input"},
    500: {"model": ErrorEnvelope, "description": "The repository failed or returned an invalid result"},
    503: {"model": ErrorEnvelope, "description": "No ReviewTaskRepository is configured (no production adapter yet)"},
}


@router.post(
    "/review-tasks/{task_id}/claim",
    dependencies=[Depends(require_scopes(SCOPE_REVIEW_TASKS_CLAIM)), Depends(_reject_unexpected_action_input)],
    response_model=ReviewTaskResource,
    summary="Claim a review task",
    description=(
        "Claims an unassigned task for the authenticated principal (the owner is never taken from the request). Claiming a "
        "task the caller already owns succeeds and changes nothing (idempotent). A task owned by someone else answers "
        "`409 TASK_CLAIMED` with `details.claimed_by` (the owner's principal ID; no display name exists) and stays theirs. "
        "A completed task answers `409 TASK_COMPLETED`. Atomic at the repository port: of two concurrent claims exactly one "
        "wins. PROVISIONAL (M5-D11 is Open): scope `review_tasks:claim` is not owner-approved; there is no claim expiry. "
        "Atomicity across processes holds only with a production adapter that implements the port's atomicity contract; "
        "none exists yet. No query parameter or body is accepted."
    ),
    responses=_ACTION_RESPONSES,
)
async def claim_review_task(
    request: Request,
    response: Response,
    task_id: str = Path(..., description="Review task identifier"),
    principal: ApiPrincipal = Depends(get_current_principal),
    service: ReviewTaskService = Depends(get_review_task_service),
) -> ReviewTaskResource:
    task = await service.claim(task_id, request_id=getattr(request.state, "request_id", None))
    response.headers["Cache-Control"] = _NO_STORE
    return ReviewTaskResource.from_record(task, principal.principal_id)


@router.post(
    "/review-tasks/{task_id}/release",
    dependencies=[Depends(require_scopes(SCOPE_REVIEW_TASKS_CLAIM)), Depends(_reject_unexpected_action_input)],
    response_model=ReviewTaskResource,
    summary="Release a review task",
    description=(
        "Returns a task owned by the authenticated principal to `unassigned`. ONLY the current owner may release: a task "
        "owned by someone else answers `409 TASK_CLAIMED` and is not changed (there is no administrator release). Releasing "
        "a task nobody owns succeeds and changes nothing (idempotent). A completed task answers `409 TASK_COMPLETED`. "
        "PROVISIONAL (M5-D11 is Open): scope `review_tasks:claim` is not owner-approved. No query parameter or body is accepted."
    ),
    responses=_ACTION_RESPONSES,
)
async def release_review_task(
    request: Request,
    response: Response,
    task_id: str = Path(..., description="Review task identifier"),
    principal: ApiPrincipal = Depends(get_current_principal),
    service: ReviewTaskService = Depends(get_review_task_service),
) -> ReviewTaskResource:
    task = await service.release(task_id, request_id=getattr(request.state, "request_id", None))
    response.headers["Cache-Control"] = _NO_STORE
    return ReviewTaskResource.from_record(task, principal.principal_id)
