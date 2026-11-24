"""
MongoDB ``ReviewTaskRepository`` (Team Member 3, Phase 2A-3).

The production adapter of the ``ReviewTaskRepository`` port (``doc_extractor.api.ports.repositories``). The port, the record
model, the transition outcomes and the in-memory test double are the contract; this module re-expresses them over MongoDB and
decides nothing new. Every operation goes through ``TenantScopedRepository``: the tenant filter is mandatory and cannot be
overridden, a task of another tenant is simply absent, and a driver failure becomes a typed ``PersistenceError``.

ATOMICITY (the port's contract). ``claim``, ``release`` and ``complete`` are each ONE conditional ``find_one_and_update``: the
precondition (status and owner) is part of the filter, so the server decides and writes in one indivisible step per
document, whichever process the caller runs in. Two concurrent claims of one unassigned task: exactly one filter matches. When
the conditional update matches nothing, a follow-up read explains why (not found, completed, owned by someone else, ...) and
that is the returned outcome. The task cannot be changed back by us between the two steps; if ANOTHER writer changed it so
that our precondition now holds, the whole step is retried (bounded, with a short back-off) instead of being misreported.

STATE WRITTEN. ``unassigned``: ``assigned_reviewer_id`` is stored as null and there is no ``completed_at``. ``claimed``: the
owner. ``completed``: ``completed_at``; the owner stays recorded as the completer. These are the invariants of
``ReviewTaskRecord``, which is also re-validated on every read, so a stored document that breaks them is a typed data error and
is never returned.

INDEXES. Only the ones declared in ``indexes.py`` are used (``rt_tenant_task_uq`` unique; ``rt_tenant_document_created``,
``rt_tenant_status_created_task`` and ``rt_tenant_reviewer_status_created_task`` non-unique). Nothing here adds a uniqueness
rule: a document may have several tasks (the lifecycle decision is open), so ``get_by_document_id`` returns the most recent
one, deterministically (``created_at`` descending, then ``task_id`` descending).

LIMITS (documented, not hidden). A list with neither a status nor an assignee filter has no index for its ordering, so the
server sorts the tenant's tasks for that query. ``metadata`` keys must be valid BSON field names (no ``.`` or leading ``$``)
and its values plain BSON-safe values; anything else is a typed ``PersistenceDataError`` on ``create``.
"""

from __future__ import annotations

import asyncio
from datetime import datetime
from enum import Enum
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple

from doc_extractor.api.contracts.records import ReviewTaskRecord, ReviewTaskStatus
from doc_extractor.api.ports.repositories import (
    ReviewTaskAssignee,
    ReviewTaskListQuery,
    ReviewTaskPage,
    ReviewTaskTransition,
    ReviewTaskTransitionResult,
)
from doc_extractor.infrastructure.mongo.connection import MongoConnection
from doc_extractor.infrastructure.mongo.errors import (
    DuplicateKeyPersistenceError,
    PersistenceUnavailableError,
    UnsafeQueryError,
)
from doc_extractor.infrastructure.mongo.indexes import REVIEW_TASKS
from doc_extractor.infrastructure.mongo.repository import MAX_PAGE_SIZE, TenantScopedRepository
from doc_extractor.infrastructure.mongo.serialization import from_document, to_document

OLDEST_FIRST: Tuple[Tuple[str, int], ...] = (("created_at", 1), ("task_id", 1))
NEWEST_FIRST: Tuple[Tuple[str, int], ...] = (("created_at", -1), ("task_id", -1))

_MAX_ATTEMPTS = 6                  # a lost race is retried; this bounds it (about 0.3 s in total with the back-off below)
_BACKOFF_SECONDS = 0.01

Decide = Callable[[ReviewTaskRecord], Optional[ReviewTaskTransition]]


class DuplicateReviewTaskError(DuplicateKeyPersistenceError, ValueError):
    """``create`` of a task whose ``(tenant, task_id)`` exists. A ``ValueError`` as well, like the contract's reference double."""


def _text(value: Any, name: str, *, non_empty: bool = False) -> str:
    if not isinstance(value, str) or (non_empty and not value.strip()):
        raise UnsafeQueryError(f"{name}_is_not_a_valid_string")
    return value


def _instant(value: Any) -> datetime:
    if not isinstance(value, datetime):
        raise UnsafeQueryError("timestamp_is_not_a_datetime")
    return value


def _value(item: Any) -> Any:
    return item.value if isinstance(item, Enum) else item


def _record(document: Mapping[str, Any]) -> ReviewTaskRecord:
    return from_document(ReviewTaskRecord, document)


class MongoReviewTaskRepository(TenantScopedRepository):
    """The ``ReviewTaskRepository`` port over MongoDB. See the module docstring."""

    def __init__(self, connection: MongoConnection) -> None:
        super().__init__(connection, REVIEW_TASKS)

    # ---- atomic transitions (API-016, provisional complete) --------------------------------------------------------------

    async def claim(self, tenant_id: str, task_id: str, reviewer_id: str, now: datetime) -> ReviewTaskTransitionResult:
        reviewer = _text(reviewer_id, "reviewer_id", non_empty=True)

        def decide(task: ReviewTaskRecord) -> Optional[ReviewTaskTransition]:
            if task.status is ReviewTaskStatus.COMPLETED:
                return ReviewTaskTransition.COMPLETED
            if task.status is ReviewTaskStatus.CLAIMED:
                return (
                    ReviewTaskTransition.ALREADY_OWNED
                    if task.assigned_reviewer_id == reviewer
                    else ReviewTaskTransition.HELD_BY_OTHER
                )
            return None                                      # unassigned: someone released it since our attempt; try again

        return await self._transition(
            tenant_id,
            task_id,
            precondition={"status": ReviewTaskStatus.UNASSIGNED.value},
            changes={"status": ReviewTaskStatus.CLAIMED.value, "assigned_reviewer_id": reviewer, "updated_at": _instant(now)},
            won=ReviewTaskTransition.CLAIMED,
            decide=decide,
        )

    async def release(self, tenant_id: str, task_id: str, reviewer_id: str, now: datetime) -> ReviewTaskTransitionResult:
        reviewer = _text(reviewer_id, "reviewer_id", non_empty=True)

        def decide(task: ReviewTaskRecord) -> Optional[ReviewTaskTransition]:
            if task.status is ReviewTaskStatus.COMPLETED:
                return ReviewTaskTransition.COMPLETED
            if task.status is ReviewTaskStatus.UNASSIGNED:
                return ReviewTaskTransition.ALREADY_UNASSIGNED
            if task.assigned_reviewer_id != reviewer:
                return ReviewTaskTransition.HELD_BY_OTHER
            return None                                      # still ours: the state moved under the first attempt; try again

        return await self._transition(
            tenant_id,
            task_id,
            precondition={"status": ReviewTaskStatus.CLAIMED.value, "assigned_reviewer_id": reviewer},
            changes={"status": ReviewTaskStatus.UNASSIGNED.value, "assigned_reviewer_id": None, "updated_at": _instant(now)},
            won=ReviewTaskTransition.RELEASED,
            decide=decide,
        )

    async def complete(self, tenant_id: str, task_id: str, reviewer_id: str, now: datetime) -> ReviewTaskTransitionResult:
        """PROVISIONAL (M5-D11 is Open), as in the port: the task RECORD only; no document, no audit."""
        reviewer = _text(reviewer_id, "reviewer_id", non_empty=True)
        moment = _instant(now)

        def decide(task: ReviewTaskRecord) -> Optional[ReviewTaskTransition]:
            if task.status is ReviewTaskStatus.COMPLETED:
                return (
                    ReviewTaskTransition.ALREADY_COMPLETED
                    if task.assigned_reviewer_id == reviewer
                    else ReviewTaskTransition.COMPLETED
                )
            if task.status is ReviewTaskStatus.UNASSIGNED:
                return ReviewTaskTransition.NOT_CLAIMED
            if task.assigned_reviewer_id != reviewer:
                return ReviewTaskTransition.HELD_BY_OTHER
            return None

        return await self._transition(
            tenant_id,
            task_id,
            precondition={"status": ReviewTaskStatus.CLAIMED.value, "assigned_reviewer_id": reviewer},
            changes={"status": ReviewTaskStatus.COMPLETED.value, "completed_at": moment, "updated_at": moment},
            won=ReviewTaskTransition.COMPLETION_RECORDED,
            decide=decide,
        )

    async def _transition(
        self,
        tenant_id: str,
        task_id: str,
        *,
        precondition: Dict[str, Any],
        changes: Dict[str, Any],
        won: ReviewTaskTransition,
        decide: Decide,
    ) -> ReviewTaskTransitionResult:
        """
        One conditional update; its outcome when it matched, otherwise the outcome ``decide`` derives from the task as it
        now is. ``decide`` returns ``None`` when the task now satisfies the precondition after all (a concurrent writer moved
        it): the step is then repeated, a bounded number of times.
        """
        task_id = _text(task_id, "task_id")
        selector = {"task_id": task_id, **precondition}
        for attempt in range(_MAX_ATTEMPTS):
            written = await self.find_one_and_update(tenant_id, selector, {"$set": changes}, return_after=True)
            if written is not None:
                return ReviewTaskTransitionResult(won, _record(written))
            current = await self.find_one(tenant_id, {"task_id": task_id})
            if current is None:
                return ReviewTaskTransitionResult(ReviewTaskTransition.NOT_FOUND, None)
            task = _record(current)
            outcome = decide(task)
            if outcome is not None:
                return ReviewTaskTransitionResult(outcome, task)
            await asyncio.sleep(_BACKOFF_SECONDS * (2 ** attempt))
        raise PersistenceUnavailableError("task_state_did_not_settle", collection=self.collection_name)

    # ---- reads ------------------------------------------------------------------------------------------------------------

    async def get_by_id(self, tenant_id: str, task_id: str) -> Optional[ReviewTaskRecord]:
        document = await self.find_one(tenant_id, {"task_id": _text(task_id, "task_id")})
        return None if document is None else _record(document)

    async def get_by_document_id(self, tenant_id: str, document_id: str) -> Optional[ReviewTaskRecord]:
        """The document's MOST RECENT task (``created_at`` descending, then ``task_id`` descending); ``None`` when it has none."""
        document = await self.find_one(
            tenant_id, {"document_id": _text(document_id, "document_id")}, sort=NEWEST_FIRST
        )
        return None if document is None else _record(document)

    async def list_page(self, tenant_id: str, query: ReviewTaskListQuery) -> ReviewTaskPage:
        selector: Dict[str, Any] = {}
        if query.status is not None:
            selector["status"] = _value(query.status)
        if query.assignee is ReviewTaskAssignee.MINE:
            selector["assigned_reviewer_id"] = query.reviewer_id
        elif query.assignee is ReviewTaskAssignee.UNASSIGNED:
            selector["assigned_reviewer_id"] = None          # equality with null: no owner, whether stored as null or absent
        if query.document_id is not None:
            selector["document_id"] = query.document_id
        total = await self.count(tenant_id, selector)
        start = (query.page - 1) * query.size
        if start >= total:                                   # a page past the end is empty, never an error (and never a huge skip)
            return ReviewTaskPage(items=(), total_items=total)
        documents = await self._window(tenant_id, selector, OLDEST_FIRST, start, query.size)
        items = tuple(_record(document) for document in documents)
        # Tasks created between the count and the read could make the page longer than the count says; never report less.
        return ReviewTaskPage(items=items, total_items=max(total, start + len(items)))

    async def list_tasks(
        self,
        tenant_id: str,
        status: Optional[str] = None,
        reviewer_id: Optional[str] = None,
        limit: int = 50,
        offset: int = 0,
    ) -> List[ReviewTaskRecord]:
        if limit < 0 or offset < 0:
            raise UnsafeQueryError("page_bounds_are_not_valid")
        selector: Dict[str, Any] = {}
        if status is not None:
            selector["status"] = _value(status)
        if reviewer_id is not None:
            selector["assigned_reviewer_id"] = reviewer_id
        if limit == 0:
            return []
        return [_record(document) for document in await self._window(tenant_id, selector, OLDEST_FIRST, offset, limit)]

    async def _window(
        self, tenant_id: str, selector: Mapping[str, Any], sort: Sequence[Tuple[str, int]], skip: int, limit: int
    ) -> List[Dict[str, Any]]:
        """``limit`` documents from ``skip``, fetched in chunks of at most the foundation's page bound."""
        documents: List[Dict[str, Any]] = []
        while len(documents) < limit:
            wanted = min(MAX_PAGE_SIZE, limit - len(documents))
            chunk = await self.find_many(tenant_id, selector, sort=sort, skip=skip + len(documents), limit=wanted)
            documents.extend(chunk)
            if len(chunk) < wanted:                          # the collection ran out
                break
        return documents

    # ---- writes -----------------------------------------------------------------------------------------------------------

    async def create(self, tenant_id: str, task: ReviewTaskRecord) -> ReviewTaskRecord:
        """
        Store ``task`` and return it as stored (UTC, millisecond precision: what a later read returns). A task naming another
        tenant raises ``TenantScopeError``; an existing ``(tenant, task_id)`` raises ``DuplicateReviewTaskError``. Both are
        ``ValueError``s, like the reference double. A document may have several tasks: nothing here forbids it.
        """
        if not isinstance(task, ReviewTaskRecord):
            raise UnsafeQueryError("task_is_not_a_review_task_record")
        document = task.model_dump()
        try:
            await self.insert_one(tenant_id, document)
        except DuplicateKeyPersistenceError:
            raise DuplicateReviewTaskError(collection=self.collection_name) from None
        return _record(to_document(document))

    async def update_status(
        self, tenant_id: str, task_id: str, status: str, reviewer_id: Optional[str] = None, notes: Optional[str] = None
    ) -> Optional[ReviewTaskRecord]:
        """
        UNCONDITIONAL as the port states (status, owner and notes are overwritten; nothing about the previous owner or state is
        checked) and NEVER to be used to claim or release. The new combination is validated with the record's invariants first,
        exactly like the reference double: an invalid one raises the validation error and nothing is written. The write is
        guarded on ``completed_at`` only, so a concurrent completion cannot leave a record that breaks those invariants; if it
        moves, the update is re-derived from the fresh state.
        """
        task_id = _text(task_id, "task_id")
        for _ in range(_MAX_ATTEMPTS):
            current = await self.find_one(tenant_id, {"task_id": task_id})
            if current is None:
                return None
            before = _record(current)
            data = before.model_dump()
            data.update(status=status, assigned_reviewer_id=reviewer_id, reviewer_notes=notes)
            after = ReviewTaskRecord.model_validate(data)
            written = await self.find_one_and_update(
                tenant_id,
                {"task_id": task_id, "completed_at": current.get("completed_at")},
                {"$set": {
                    "status": after.status.value,
                    "assigned_reviewer_id": after.assigned_reviewer_id,
                    "reviewer_notes": after.reviewer_notes,
                }},
                return_after=True,
            )
            if written is not None:
                return _record(written)
        raise PersistenceUnavailableError("task_state_did_not_settle", collection=self.collection_name)
