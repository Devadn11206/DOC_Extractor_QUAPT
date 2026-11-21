"""
Pure filtering, ordering and paging of review tasks (API-005, Module M5 / Phase 3, Slice 1), PROVISIONAL.

No I/O. This is the reference implementation of the ``ReviewTaskRepository.list_page`` semantics, used by the in-memory test
double; a database adapter would express the same rules in its query. Rules (decision M5-D11 is Open):

- Filters combine with AND: ``status`` equality, ``assignee`` (mine / unassigned / all) and ``document_id`` equality.
- Order is OLDEST FIRST (PRD DOCX-SCR-004, "Next task (oldest first)"): ``created_at`` ascending as instants (a naive
  timestamp is read as UTC), then ``task_id`` ascending, so the order is total and repeatable. ``priority`` is not used.
- ``total_items`` counts every matching task, not only the page. A page past the end is empty, not an error.
- The caller supplies ONLY the tenant's own tasks. This function does not (and cannot) scope by tenant.
"""

from datetime import datetime, timezone
from typing import Iterable, List

from doc_extractor.api.contracts.records import ReviewTaskRecord
from doc_extractor.api.ports.repositories import ReviewTaskAssignee, ReviewTaskListQuery, ReviewTaskPage


def instant_of(value: datetime) -> datetime:
    """An aware instant: a naive stored timestamp is read as UTC (the same rule as the document listing)."""
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


def _matches(task: ReviewTaskRecord, query: ReviewTaskListQuery) -> bool:
    if query.status is not None and task.status is not query.status:
        return False
    if query.assignee is ReviewTaskAssignee.MINE and task.assigned_reviewer_id != query.reviewer_id:
        return False
    if query.assignee is ReviewTaskAssignee.UNASSIGNED and task.assigned_reviewer_id is not None:
        return False
    if query.document_id is not None and task.document_id != query.document_id:
        return False
    return True


def build_review_task_page(tasks: Iterable[ReviewTaskRecord], query: ReviewTaskListQuery) -> ReviewTaskPage:
    """Filter, order (oldest first) and page ``tasks`` for ``query``. Pure and deterministic."""
    matching: List[ReviewTaskRecord] = [task for task in tasks if _matches(task, query)]
    matching.sort(key=lambda task: (instant_of(task.created_at), task.task_id))
    start = (query.page - 1) * query.size
    return ReviewTaskPage(items=tuple(matching[start:start + query.size]), total_items=len(matching))
