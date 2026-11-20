"""
Review-task queue service (Module M5 / Phase 3, Slice 1: API-005 list, API-016 claim and release), PROVISIONAL.

A thin, tenant-bound application boundary over the ``ReviewTaskRepository`` port. The tenant and the principal come ONLY from
the authenticated request (the dependency builds this service from the principal); no caller-supplied tenant or reviewer is
ever read. The service decides nothing about the task's state itself: claim and release are single atomic repository calls
(see the port's atomicity contract), and this class only maps their returned outcome to the public error or task.

Semantics (decision M5-D11 is Open; nothing here is owner-approved):

- claim: unassigned -> claimed by the caller; already owned by the caller -> success, nothing changes (idempotent); owned by
  another principal -> 409 ``TASK_CLAIMED`` naming the owner's principal ID; completed -> 409 ``TASK_COMPLETED``.
- release: only the current owner may release; owned by another principal -> 409 ``TASK_CLAIMED`` (nothing changes);
  unassigned -> success, nothing changes (idempotent, so a retry by the owner is safe); completed -> 409 ``TASK_COMPLETED``.
- there is NO automatic claim expiry, no administrator release and no reviewer display name.

Defence in depth: whatever an adapter returns is checked before it is shown. A record of another tenant, of another task, or
in a state that contradicts the outcome is an ADAPTER FAULT (generic 500, fixed reason), never forwarded to the client.

Logging (SEC-012): constant messages with a fixed reason or outcome code, the request ID and the (path-validated) task ID.
Never a principal ID, a tenant ID, a note, a field value or an exception text.
"""

import logging
from datetime import datetime
from typing import Any, Callable, Optional

from doc_extractor.api.contracts.records import ReviewTaskRecord, ReviewTaskStatus, utc_now
from doc_extractor.api.contracts.review_tasks import validate_task_id
from doc_extractor.api.exceptions import (
    ReviewTaskClaimedError,
    ReviewTaskCompletedError,
    ReviewTaskNotFoundError,
    TenantAccessDeniedError,
)
from doc_extractor.api.ports.repositories import (
    ReviewTaskAssignee,
    ReviewTaskListQuery,
    ReviewTaskPage,
    ReviewTaskRepository,
    ReviewTaskTransition,
    ReviewTaskTransitionResult,
)
from doc_extractor.exceptions import (
    CrossTenantStorageAccessError,
    StorageError,
    TenantMismatchError,
)

logger = logging.getLogger("doc_extractor.api.review_tasks")

REASON_REPOSITORY_FAULT = "review_task_repository_fault"
REASON_ADAPTER_CONTRACT = "review_task_adapter_contract_violation"

_CROSS_TENANT_SIGNALS = (TenantMismatchError, CrossTenantStorageAccessError, TenantAccessDeniedError)


def _fault(reason: str, request_id: Optional[str]) -> StorageError:
    """A generic 500: the handler logs the fixed reason server-side; the client sees INTERNAL_ERROR only."""
    return StorageError("Review task operation failed.", request_id=request_id, details={"reason": reason})


class ReviewTaskService:
    """Tenant- and principal-bound review queue operations."""

    def __init__(
        self,
        repository: ReviewTaskRepository,
        tenant_id: str,
        principal_id: str,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        if not isinstance(tenant_id, str) or not tenant_id.strip():
            raise ValueError("tenant_id is required")
        if not isinstance(principal_id, str) or not principal_id.strip():
            raise ValueError("principal_id is required")
        self._repository = repository
        self._tenant_id = tenant_id
        self._principal_id = principal_id
        self._clock = clock

    # ---- list (API-005) -----------------------------------------------------------------------------------------------

    async def list_tasks(
        self,
        *,
        status: Optional[ReviewTaskStatus],
        assignee: ReviewTaskAssignee,
        document_id: Optional[str],
        page: int,
        size: int,
        request_id: Optional[str] = None,
    ) -> ReviewTaskPage:
        """One page of the caller's tenant's tasks, oldest first. ``assignee=mine`` means the authenticated principal."""
        query = ReviewTaskListQuery(
            status=status,
            assignee=assignee,
            reviewer_id=self._principal_id if assignee is ReviewTaskAssignee.MINE else None,
            document_id=document_id,
            page=page,
            size=size,
        )
        result = await self._call(self._repository.list_page(self._tenant_id, query), request_id)
        items = getattr(result, "items", None)
        total = getattr(result, "total_items", None)
        valid = (
            isinstance(result, ReviewTaskPage)
            and isinstance(items, tuple)
            and isinstance(total, int) and not isinstance(total, bool)
            and total >= len(items) and len(items) <= size
            and all(self._belongs_to_tenant(task) for task in items)
        )
        if not valid:
            raise _fault(REASON_ADAPTER_CONTRACT, request_id)
        return result

    # ---- claim and release (API-016) ----------------------------------------------------------------------------------

    async def claim(self, task_id: str, request_id: Optional[str] = None) -> ReviewTaskRecord:
        """Claim ``task_id`` for the authenticated principal (idempotent for the current owner)."""
        task_id = validate_task_id(task_id)
        result = await self._call(
            self._repository.claim(self._tenant_id, task_id, self._principal_id, self._clock()), request_id
        )
        return self._resolve("claim", task_id, result, request_id)

    async def release(self, task_id: str, request_id: Optional[str] = None) -> ReviewTaskRecord:
        """Release ``task_id``: only its current owner may."""
        task_id = validate_task_id(task_id)
        result = await self._call(
            self._repository.release(self._tenant_id, task_id, self._principal_id, self._clock()), request_id
        )
        return self._resolve("release", task_id, result, request_id)

    # ---- internals ----------------------------------------------------------------------------------------------------

    async def _call(self, awaitable: Any, request_id: Optional[str]) -> Any:
        """Await a repository call. Any failure becomes a sanitized error; cross-tenant signals are the concealed 404."""
        try:
            return await awaitable
        except _CROSS_TENANT_SIGNALS:
            raise ReviewTaskNotFoundError(request_id=request_id) from None
        except Exception as exc:                                   # noqa: BLE001 - never propagates its text
            logger.error(
                "Review task repository fault (reason=%s, exception=%s, request_id=%s)",
                REASON_REPOSITORY_FAULT, type(exc).__name__, request_id,
            )
            raise _fault(REASON_REPOSITORY_FAULT, request_id) from None

    def _belongs_to_tenant(self, task: Any) -> bool:
        return isinstance(task, ReviewTaskRecord) and task.tenant_id == self._tenant_id

    def _resolve(
        self, operation: str, task_id: str, result: ReviewTaskTransitionResult, request_id: Optional[str]
    ) -> ReviewTaskRecord:
        """Map one atomic outcome to the task or to the public error; verify what the adapter returned first."""
        outcome = getattr(result, "outcome", None)
        task = getattr(result, "task", None)
        if not isinstance(outcome, ReviewTaskTransition):
            raise _fault(REASON_ADAPTER_CONTRACT, request_id)

        logger.info(
            "Review task %s (outcome=%s, task_id=%s, request_id=%s)", operation, outcome.value, task_id, request_id
        )
        if outcome is ReviewTaskTransition.NOT_FOUND:
            raise ReviewTaskNotFoundError(request_id=request_id)

        if not self._belongs_to_tenant(task) or task.task_id != task_id:     # type: ignore[union-attr]
            raise _fault(REASON_ADAPTER_CONTRACT, request_id)

        mine = task.assigned_reviewer_id == self._principal_id               # type: ignore[union-attr]
        if outcome is ReviewTaskTransition.HELD_BY_OTHER:
            if task.status is not ReviewTaskStatus.CLAIMED or task.assigned_reviewer_id is None or mine:  # type: ignore[union-attr]
                raise _fault(REASON_ADAPTER_CONTRACT, request_id)
            raise ReviewTaskClaimedError(
                request_id=request_id, details={"claimed_by": task.assigned_reviewer_id}  # type: ignore[union-attr]
            )
        if outcome is ReviewTaskTransition.COMPLETED:
            if task.status is not ReviewTaskStatus.COMPLETED:                # type: ignore[union-attr]
                raise _fault(REASON_ADAPTER_CONTRACT, request_id)
            raise ReviewTaskCompletedError(request_id=request_id)

        expected = {
            "claim": {ReviewTaskTransition.CLAIMED, ReviewTaskTransition.ALREADY_OWNED},
            "release": {ReviewTaskTransition.RELEASED, ReviewTaskTransition.ALREADY_UNASSIGNED},
        }[operation]
        if outcome not in expected:
            raise _fault(REASON_ADAPTER_CONTRACT, request_id)
        if operation == "claim":
            consistent = task.status is ReviewTaskStatus.CLAIMED and mine    # type: ignore[union-attr]
        else:
            consistent = task.status is ReviewTaskStatus.UNASSIGNED          # type: ignore[union-attr]
        if not consistent:
            raise _fault(REASON_ADAPTER_CONTRACT, request_id)
        return task                                                          # type: ignore[return-value]
