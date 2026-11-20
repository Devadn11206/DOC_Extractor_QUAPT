"""
Pure filtering, sorting and pagination of validated document records (API-015, Module M5 / Milestone 2D).

No filesystem, database or network access happens here: the input is a sequence of already validated
``DocumentRecord`` objects (see ``DocumentReadService.list_documents``) and the output is one page plus the metadata
that belongs to it. Everything is computed from a single in-memory snapshot, so ``total_items``, ``stats`` and the page
itself always agree with each other within one call.

PROVISIONAL contract (docs/m5-open-decisions.md, M5-D06, M2D-D01):

- Filters: persisted ``status`` (equality), ``created_at`` range (both bounds inclusive, compared as instants; a naive
  stored timestamp is read as UTC), and a case-sensitive ``document_id`` prefix.
- Order: the chosen field in the chosen direction (default ``created_at`` descending), then ALWAYS ``document_id``
  ASCENDING as the tie-breaker, whichever direction was asked for. The order is therefore total and repeatable for one
  data set; ascending and descending are not exact mirror images of each other when keys tie.
- ``status`` sorts by the alphabetical order of its stored string value, not by workflow order.
- ``stats`` is the persisted-status distribution of ALL records, before any filter (see ``DocumentStats``).
- Nothing keeps a snapshot between requests: a record added between two page requests can move records across a page
  boundary.
"""

import math
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Dict, List, Optional, Sequence, Tuple

from doc_extractor.api.contracts.documents import parse_stored_timestamp
from doc_extractor.models import DocumentRecord, DocumentStatus

DEFAULT_PAGE_SIZE = 10
MAX_PAGE_SIZE = 100            # provisional: the PRD names only the default of 10


_COUNTED_STATUSES = ("processing", "needs_review", "auto_approved", "failed")


class DocumentSortField(str, Enum):
    """The only fields a list can be sorted by: each is a persisted, authoritative field."""

    CREATED_AT = "created_at"
    DOCUMENT_ID = "document_id"
    PAGE_COUNT = "page_count"
    FILE_SIZE_BYTES = "file_size_bytes"
    STATUS = "status"


class SortOrder(str, Enum):
    ASC = "asc"
    DESC = "desc"


@dataclass(frozen=True)
class DocumentListQuery:
    """A validated list request. Dates are aware instants and both bounds are inclusive."""

    status: Optional[DocumentStatus] = None
    created_from: Optional[datetime] = None
    created_to: Optional[datetime] = None
    id_prefix: Optional[str] = None
    sort: DocumentSortField = DocumentSortField.CREATED_AT
    order: SortOrder = SortOrder.DESC
    page: int = 1
    size: int = DEFAULT_PAGE_SIZE


@dataclass(frozen=True)
class DocumentListing:
    """One page of records plus the metadata computed from the same snapshot."""

    records: Tuple[DocumentRecord, ...]
    page: int
    size: int
    total_items: int
    total_pages: int
    stats: Dict[str, int]


def compute_document_stats(records: Sequence[DocumentRecord]) -> Dict[str, int]:
    """
    The five PRD keys over ``records``. ``total`` counts every record; each other key counts the records whose PERSISTED
    status equals it. ``reviewed`` and ``rejected`` records count only toward ``total``.
    """
    stats = {"total": len(records), **{key: 0 for key in _COUNTED_STATUSES}}
    for record in records:
        if record.status.value in _COUNTED_STATUSES:
            stats[record.status.value] += 1
    return stats


def _sort_key(field: DocumentSortField):
    if field is DocumentSortField.CREATED_AT:
        return lambda row: row[1]
    if field is DocumentSortField.DOCUMENT_ID:
        return lambda row: row[0].document_id
    if field is DocumentSortField.PAGE_COUNT:
        return lambda row: row[0].page_count
    if field is DocumentSortField.FILE_SIZE_BYTES:
        return lambda row: row[0].file_size_bytes
    return lambda row: row[0].status.value


def build_document_listing(records: Sequence[DocumentRecord], query: DocumentListQuery) -> DocumentListing:
    """
    Filter, sort and paginate ``records`` for ``query``. Pure and deterministic. An out-of-range page is not an error:
    it yields no records and the true totals.
    """
    if query.page < 1 or query.size < 1:
        raise ValueError("page and size must be at least 1")

    needs_instant = (
        query.created_from is not None or query.created_to is not None or query.sort is DocumentSortField.CREATED_AT
    )
    rows: List[Tuple[DocumentRecord, Optional[datetime]]] = [
        (record, parse_stored_timestamp(record.created_at) if needs_instant else None) for record in records
    ]
    stats = compute_document_stats(records)                    # before any filter

    def keep(row: Tuple[DocumentRecord, Optional[datetime]]) -> bool:
        record, instant = row
        if query.status is not None and record.status is not query.status:
            return False
        if query.created_from is not None and instant < query.created_from:
            return False
        if query.created_to is not None and instant > query.created_to:
            return False
        if query.id_prefix is not None and not record.document_id.startswith(query.id_prefix):
            return False
        return True

    matching = [row for row in rows if keep(row)]
    matching.sort(key=lambda row: row[0].document_id)          # tie-breaker first: the next sort is stable
    matching.sort(key=_sort_key(query.sort), reverse=query.order is SortOrder.DESC)

    total_items = len(matching)
    total_pages = math.ceil(total_items / query.size)
    start = (query.page - 1) * query.size
    page_rows = matching[start:start + query.size]
    return DocumentListing(
        records=tuple(row[0] for row in page_rows),
        page=query.page,
        size=query.size,
        total_items=total_items,
        total_pages=total_pages,
        stats=stats,
    )
