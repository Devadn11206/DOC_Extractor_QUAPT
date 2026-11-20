"""
In-memory ``ReviewTaskRepository`` TEST DOUBLE (Phase 3, Slice 1). NOT a production adapter.

It exists only to exercise the port's contract in tests. It is deliberately kept under ``tests/`` and not in the
application package, so it cannot be wired in by accident: production persistence does not exist yet (Team Member 3), and
the application container leaves the port unconfigured (every review-task route answers 503 until an adapter is injected).

ATOMICITY BOUNDARY: ``claim`` and ``release`` read the task, decide and write it under ONE ``threading.Lock``, with no
``await`` inside the critical section. That makes them atomic among threads of ONE process (and, because there is no
await inside, among coroutines of one event loop). It proves nothing about atomicity ACROSS PROCESSES or across
a database: that needs a production adapter implementing the port's atomicity contract (a conditional UPDATE or a row lock).

``RacyReviewTaskRepository`` is the same double WITHOUT the guarantee (a check, a yield, then a write). The tests use it to
prove that the concurrency tests can actually detect a non-atomic implementation.
"""

import asyncio
import threading
from datetime import datetime
from typing import Dict, Iterable, List, Optional, Tuple

from doc_extractor.api.contracts.records import ReviewTaskRecord, ReviewTaskStatus
from doc_extractor.api.ports.repositories import (
    ReviewTaskListQuery,
    ReviewTaskPage,
    ReviewTaskTransition,
    ReviewTaskTransitionResult,
)
from doc_extractor.api.services.review_task_listing import build_review_task_page


def _changed(task: ReviewTaskRecord, **changes) -> ReviewTaskRecord:
    """A re-VALIDATED copy (``model_copy`` would skip the state invariants)."""
    data = task.model_dump()
    data.update(changes)
    return ReviewTaskRecord.model_validate(data)


class InMemoryReviewTaskRepository:
    """Atomic (per process) in-memory implementation of the ReviewTaskRepository port, for tests only."""

    def __init__(self, tasks: Iterable[ReviewTaskRecord] = ()) -> None:
        self._lock = threading.Lock()
        self._tasks: Dict[Tuple[str, str], ReviewTaskRecord] = {}
        for task in tasks:
            self._tasks[(task.tenant_id, task.task_id)] = task
        self.write_count = 0                                  # number of state writes (idempotent calls must not add to it)

    # ---- helpers for tests ----------------------------------------------------------------------------------------------

    def snapshot(self, tenant_id: str, task_id: str) -> Optional[ReviewTaskRecord]:
        with self._lock:
            task = self._tasks.get((tenant_id, task_id))
            return task.model_copy(deep=True) if task is not None else None

    def _tenant_tasks(self, tenant_id: str) -> List[ReviewTaskRecord]:
        return [task for (tenant, _), task in self._tasks.items() if tenant == tenant_id]

    # ---- the atomic API-016 operations ----------------------------------------------------------------------------------

    async def claim(self, tenant_id: str, task_id: str, reviewer_id: str, now: datetime) -> ReviewTaskTransitionResult:
        with self._lock:                                      # check, decide and write: one indivisible step
            task = self._tasks.get((tenant_id, task_id))
            if task is None:
                return ReviewTaskTransitionResult(ReviewTaskTransition.NOT_FOUND, None)
            if task.status is ReviewTaskStatus.COMPLETED:
                return ReviewTaskTransitionResult(ReviewTaskTransition.COMPLETED, task.model_copy(deep=True))
            if task.status is ReviewTaskStatus.CLAIMED:
                outcome = (
                    ReviewTaskTransition.ALREADY_OWNED
                    if task.assigned_reviewer_id == reviewer_id
                    else ReviewTaskTransition.HELD_BY_OTHER
                )
                return ReviewTaskTransitionResult(outcome, task.model_copy(deep=True))
            updated = _changed(task, status=ReviewTaskStatus.CLAIMED, assigned_reviewer_id=reviewer_id, updated_at=now)
            self._tasks[(tenant_id, task_id)] = updated
            self.write_count += 1
            return ReviewTaskTransitionResult(ReviewTaskTransition.CLAIMED, updated.model_copy(deep=True))

    async def release(self, tenant_id: str, task_id: str, reviewer_id: str, now: datetime) -> ReviewTaskTransitionResult:
        with self._lock:
            task = self._tasks.get((tenant_id, task_id))
            if task is None:
                return ReviewTaskTransitionResult(ReviewTaskTransition.NOT_FOUND, None)
            if task.status is ReviewTaskStatus.COMPLETED:
                return ReviewTaskTransitionResult(ReviewTaskTransition.COMPLETED, task.model_copy(deep=True))
            if task.status is ReviewTaskStatus.UNASSIGNED:
                return ReviewTaskTransitionResult(ReviewTaskTransition.ALREADY_UNASSIGNED, task.model_copy(deep=True))
            if task.assigned_reviewer_id != reviewer_id:
                return ReviewTaskTransitionResult(ReviewTaskTransition.HELD_BY_OTHER, task.model_copy(deep=True))
            updated = _changed(task, status=ReviewTaskStatus.UNASSIGNED, assigned_reviewer_id=None, updated_at=now)
            self._tasks[(tenant_id, task_id)] = updated
            self.write_count += 1
            return ReviewTaskTransitionResult(ReviewTaskTransition.RELEASED, updated.model_copy(deep=True))

    async def list_page(self, tenant_id: str, query: ReviewTaskListQuery) -> ReviewTaskPage:
        with self._lock:
            tasks = [task.model_copy(deep=True) for task in self._tenant_tasks(tenant_id)]
        return build_review_task_page(tasks, query)

    # ---- the rest of the port (not used by this slice's routes) ----------------------------------------------------------

    async def get_by_id(self, tenant_id: str, task_id: str) -> Optional[ReviewTaskRecord]:
        return self.snapshot(tenant_id, task_id)

    async def get_by_document_id(self, tenant_id: str, document_id: str) -> Optional[ReviewTaskRecord]:
        with self._lock:
            for task in self._tenant_tasks(tenant_id):
                if task.document_id == document_id:
                    return task.model_copy(deep=True)
        return None

    async def create(self, tenant_id: str, task: ReviewTaskRecord) -> ReviewTaskRecord:
        if task.tenant_id != tenant_id:
            raise ValueError("task.tenant_id must equal tenant_id")
        with self._lock:
            if (tenant_id, task.task_id) in self._tasks:
                raise ValueError("duplicate task")
            self._tasks[(tenant_id, task.task_id)] = task
            return task.model_copy(deep=True)

    async def update_status(
        self, tenant_id: str, task_id: str, status: str, reviewer_id: Optional[str] = None, notes: Optional[str] = None
    ) -> Optional[ReviewTaskRecord]:
        """UNCONDITIONAL write (as the port documents): never used for claim or release."""
        with self._lock:
            task = self._tasks.get((tenant_id, task_id))
            if task is None:
                return None
            updated = _changed(task, status=status, assigned_reviewer_id=reviewer_id, reviewer_notes=notes)
            self._tasks[(tenant_id, task_id)] = updated
            self.write_count += 1
            return updated.model_copy(deep=True)

    async def list_tasks(
        self, tenant_id: str, status: Optional[str] = None, reviewer_id: Optional[str] = None, limit: int = 50, offset: int = 0
    ) -> List[ReviewTaskRecord]:
        with self._lock:
            tasks = [
                task.model_copy(deep=True)
                for task in self._tenant_tasks(tenant_id)
                if (status is None or task.status.value == status) and (reviewer_id is None or task.assigned_reviewer_id == reviewer_id)
            ]
        tasks.sort(key=lambda task: (task.created_at, task.task_id))
        return tasks[offset:offset + limit]


class RacyReviewTaskRepository(InMemoryReviewTaskRepository):
    """
    The SAME double without the atomicity guarantee: it reads the state, YIELDS, then writes. Used only to prove that the
    concurrency tests detect a non-atomic ``claim``; it must never satisfy them.
    """

    async def claim(self, tenant_id: str, task_id: str, reviewer_id: str, now: datetime) -> ReviewTaskTransitionResult:
        task = self.snapshot(tenant_id, task_id)             # read
        if task is None:
            return ReviewTaskTransitionResult(ReviewTaskTransition.NOT_FOUND, None)
        if task.status is not ReviewTaskStatus.UNASSIGNED:
            return await super().claim(tenant_id, task_id, reviewer_id, now)
        await asyncio.sleep(0)                               # another claim interleaves here
        with self._lock:                                     # write: blind overwrite, no re-check
            current = self._tasks[(tenant_id, task_id)]
            self._tasks[(tenant_id, task_id)] = _changed(
                current, status=ReviewTaskStatus.CLAIMED, assigned_reviewer_id=reviewer_id, updated_at=now
            )
            self.write_count += 1
            return ReviewTaskTransitionResult(ReviewTaskTransition.CLAIMED, self._tasks[(tenant_id, task_id)].model_copy(deep=True))
