"""
Repository protocol interfaces for the Document Extraction Engine (Module M5).

Adheres to:
- Hexagonal architecture / ports and adapters.
- Explicit method signatures and typed contracts.
- Mandatory tenant scoping on all data operations to enforce multi-tenant isolation.
- No concrete database adapters implemented in this phase.
"""

from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Any, Dict, List, Optional, Protocol, Tuple, runtime_checkable

from doc_extractor.models import DocumentRecord, DocumentStatus, IngestionState
from doc_extractor.api.contracts.records import (
    ApiKeyRecord,
    AuditEventRecord,
    ReviewTaskRecord,
    ReviewTaskStatus,
    WebhookDeliveryRecord,
    WebhookSubscriptionRecord,
)


@runtime_checkable
class DocumentMetadataRepository(Protocol):
    """
    Repository port for the durable document records.

    The record type is Module A's canonical ``doc_extractor.models.DocumentRecord`` (frozen; the SCH-012 status and the
    internal ingestion state live there). The API never keeps a competing document model: HTTP responses are built
    with ``DocumentResource.from_record``. Implementations (Team Member 3) MUST scope every call by ``tenant_id`` and
    treat a record of another tenant as absent (SEC-006: 404, never 403).
    """

    async def get_by_id(self, tenant_id: str, document_id: str) -> Optional[DocumentRecord]:
        """Retrieve a single document record scoped strictly to the tenant."""
        ...

    async def create(self, tenant_id: str, record: DocumentRecord) -> DocumentRecord:
        """Persist a new document record; ``record.tenant_id`` must equal ``tenant_id``."""
        ...

    async def update_status(
        self,
        tenant_id: str,
        document_id: str,
        status: DocumentStatus,
        internal_state: Optional[IngestionState] = None,
        error: Optional[Dict[str, Any]] = None,
        page_count: Optional[int] = None,
    ) -> Optional[DocumentRecord]:
        """
        Move a document to a new SCH-012 status (the legal moves are ``doc_extractor.models.VALID_STATUS_TRANSITIONS``)
        and return the updated record, or None when the tenant has no such document. ``error`` is the API-010 error
        dict of Module A (code, message, request_id, details).
        """
        ...

    async def list_by_tenant(
        self,
        tenant_id: str,
        status: Optional[DocumentStatus] = None,
        limit: int = 50,
        offset: int = 0,
    ) -> List[DocumentRecord]:
        """List documents belonging exclusively to the given tenant with optional status filter."""
        ...

    async def delete(self, tenant_id: str, document_id: str) -> bool:
        """Delete a document record if owned by tenant."""
        ...


class ReviewTaskAssignee(str, Enum):
    """The assignee filter of the review queue (PRD DOCX-SCR-004: Mine, Unassigned, All)."""

    ALL = "all"
    MINE = "mine"
    UNASSIGNED = "unassigned"


@dataclass(frozen=True)
class ReviewTaskListQuery:
    """
    A validated, tenant-scoped list request for the review queue (API-005). ``reviewer_id`` is the authenticated
    principal's ID and is required exactly when ``assignee`` is ``MINE``. Paging is 1-based.
    """

    status: Optional[ReviewTaskStatus] = None
    assignee: ReviewTaskAssignee = ReviewTaskAssignee.ALL
    reviewer_id: Optional[str] = None
    document_id: Optional[str] = None
    page: int = 1
    size: int = 10

    def __post_init__(self) -> None:
        if self.page < 1 or self.size < 1:
            raise ValueError("page and size must be at least 1")
        if (self.assignee is ReviewTaskAssignee.MINE) != (self.reviewer_id is not None):
            raise ValueError("reviewer_id is required exactly when assignee is MINE")


@dataclass(frozen=True)
class ReviewTaskPage:
    """One page of tasks (oldest first) and the number of tasks matching the filters across ALL pages."""

    items: Tuple[ReviewTaskRecord, ...]
    total_items: int


class ReviewTaskTransition(str, Enum):
    """
    The outcome of an atomic claim or release. A domain outcome is RETURNED, never raised, so a repository adapter reports
    exactly what happened under its own atomic step (infrastructure faults are the only exceptions it raises).
    """

    CLAIMED = "claimed"                          # claim: was unassigned, now owned by the caller
    ALREADY_OWNED = "already_owned"              # claim: the caller already owns it (idempotent; nothing was written)
    RELEASED = "released"                        # release: was owned by the caller, now unassigned
    ALREADY_UNASSIGNED = "already_unassigned"    # release: nobody owns it (idempotent; nothing was written)
    HELD_BY_OTHER = "held_by_other"              # claim or release: another principal owns it (nothing was written)
    COMPLETED = "completed"                      # claim or release: the task is completed (nothing was written)
    NOT_FOUND = "not_found"                      # no such task for this tenant (another tenant's task is absent too)


@dataclass(frozen=True)
class ReviewTaskTransitionResult:
    """``task`` is the state AFTER the atomic step (unchanged for every outcome that wrote nothing); None only for NOT_FOUND."""

    outcome: ReviewTaskTransition
    task: Optional[ReviewTaskRecord] = None


@runtime_checkable
class ReviewTaskRepository(Protocol):
    """
    Repository port for human-in-the-loop review workbench tasks.

    PRODUCTION PERSISTENCE DOES NOT EXIST YET: no adapter implements this port (Team Member 3), so the application
    container leaves it unconfigured and every review-task route answers a typed 503 until one is injected. The only
    implementation in the repository is the in-memory test double in ``tests/review_task_double.py``.

    Every method is scoped by ``tenant_id``; a task of another tenant is absent (SEC-006).

    ATOMICITY CONTRACT of ``claim`` and ``release``: the read of the task's current state, the decision and the write MUST
    be one indivisible step per task (for a database: a single conditional UPDATE or a row lock held across the check and
    the write; for a store without transactions: a compare-and-set on a version). Two concurrent ``claim`` calls for one
    unassigned task MUST yield exactly one ``CLAIMED`` and the other ``HELD_BY_OTHER``, whichever process they run in.
    A check in the API layer followed by ``update_status`` does NOT satisfy this, which is why ``update_status`` (an
    unconditional write) must never be used for claim or release.
    """

    async def claim(
        self, tenant_id: str, task_id: str, reviewer_id: str, now: datetime
    ) -> ReviewTaskTransitionResult:
        """
        Atomically give an UNASSIGNED task to ``reviewer_id`` (status ``claimed``, ``updated_at`` = ``now``).
        Already owned by ``reviewer_id``: ``ALREADY_OWNED``, nothing written. Owned by someone else: ``HELD_BY_OTHER``.
        Completed: ``COMPLETED``. Absent for the tenant: ``NOT_FOUND``.
        """
        ...

    async def release(
        self, tenant_id: str, task_id: str, reviewer_id: str, now: datetime
    ) -> ReviewTaskTransitionResult:
        """
        Atomically return a task OWNED BY ``reviewer_id`` to ``unassigned`` (``updated_at`` = ``now``). Only the current owner
        may release. Owned by someone else: ``HELD_BY_OTHER``, nothing written. Already unassigned: ``ALREADY_UNASSIGNED``,
        nothing written. Completed: ``COMPLETED``. Absent for the tenant: ``NOT_FOUND``.
        """
        ...

    async def list_page(self, tenant_id: str, query: ReviewTaskListQuery) -> ReviewTaskPage:
        """
        One page of the tenant's tasks matching ``query``, OLDEST FIRST: ``created_at`` ascending as instants (a naive
        timestamp is read as UTC), then ``task_id`` ascending as the tie-breaker, so the order is total and repeatable.
        Filters (all optional, combined with AND): ``status`` equality; ``assignee`` (``MINE``: owned by
        ``query.reviewer_id``; ``UNASSIGNED``: no owner; ``ALL``); ``document_id`` equality. ``total_items`` counts every
        match, not only this page. A page past the end is empty, not an error. A database adapter should filter, order and
        page in the query itself.
        """
        ...

    async def get_by_id(self, tenant_id: str, task_id: str) -> Optional[ReviewTaskRecord]:
        """Retrieve a review task by its ID scoped to the tenant."""
        ...

    async def get_by_document_id(self, tenant_id: str, document_id: str) -> Optional[ReviewTaskRecord]:
        """Retrieve the review task associated with a specific document."""
        ...

    async def create(self, tenant_id: str, task: ReviewTaskRecord) -> ReviewTaskRecord:
        """Create a new human review task."""
        ...

    async def update_status(
        self,
        tenant_id: str,
        task_id: str,
        status: str,
        reviewer_id: Optional[str] = None,
        notes: Optional[str] = None,
    ) -> Optional[ReviewTaskRecord]:
        """
        Update review task status, assigned reviewer, and review outcome notes. An UNCONDITIONAL write: it is not atomic
        with any earlier read and MUST NOT be used to claim or release a task (use ``claim`` / ``release``).
        """
        ...

    async def list_tasks(
        self,
        tenant_id: str,
        status: Optional[str] = None,
        reviewer_id: Optional[str] = None,
        limit: int = 50,
        offset: int = 0,
    ) -> List[ReviewTaskRecord]:
        """List review tasks for the tenant with pagination and optional filtering."""
        ...


@runtime_checkable
class ApiKeyRepository(Protocol):
    """Repository port for API key credential storage and lookup (TASK-5.7)."""

    async def get_by_key_hash(self, key_hash: str) -> Optional[ApiKeyRecord]:
        """Look up an active API key by its cryptographic hash for authentication."""
        ...

    async def get_by_id(self, tenant_id: str, key_id: str) -> Optional[ApiKeyRecord]:
        """Retrieve key metadata by ID scoped to the tenant."""
        ...

    async def create(self, tenant_id: str, key_record: ApiKeyRecord) -> ApiKeyRecord:
        """Register a newly generated API key for a tenant."""
        ...

    async def revoke(self, tenant_id: str, key_id: str) -> bool:
        """Revoke an active API key immediately."""
        ...

    async def list_keys(
        self,
        tenant_id: str,
        limit: int = 50,
        offset: int = 0,
    ) -> List[ApiKeyRecord]:
        """List all API keys belonging to the tenant."""
        ...

    async def record_last_used(
        self,
        tenant_id: str,
        key_id: str,
        last_used_at: datetime,
    ) -> bool:
        """Record or update the last-used timestamp for an active API key."""
        ...


@runtime_checkable
class WebhookSubscriptionRepository(Protocol):
    """Repository port for outbound webhook subscriptions (TASK-5.6)."""

    async def get_by_id(self, tenant_id: str, subscription_id: str) -> Optional[WebhookSubscriptionRecord]:
        """Retrieve a webhook subscription by ID scoped to the tenant."""
        ...

    async def list_by_tenant(
        self,
        tenant_id: str,
        event_type: Optional[str] = None,
    ) -> List[WebhookSubscriptionRecord]:
        """List active webhook subscriptions for a tenant, optionally filtered by event type."""
        ...

    async def create(
        self,
        tenant_id: str,
        subscription: WebhookSubscriptionRecord,
    ) -> WebhookSubscriptionRecord:
        """Register a new webhook subscription for a tenant."""
        ...

    async def update(
        self,
        tenant_id: str,
        subscription_id: str,
        update_data: Dict[str, Any],
    ) -> Optional[WebhookSubscriptionRecord]:
        """Update subscription properties (URL, event types, active status)."""
        ...

    async def delete(self, tenant_id: str, subscription_id: str) -> bool:
        """Delete a webhook subscription."""
        ...


@runtime_checkable
class WebhookDeliveryRepository(Protocol):
    """Repository port for tracking webhook delivery attempts and audit logs."""

    async def record_delivery(
        self,
        tenant_id: str,
        delivery: WebhookDeliveryRecord,
    ) -> WebhookDeliveryRecord:
        """Record an outbound delivery attempt."""
        ...

    async def get_by_id(
        self,
        tenant_id: str,
        delivery_id: str,
    ) -> Optional[WebhookDeliveryRecord]:
        """Retrieve a delivery attempt record by ID."""
        ...

    async def list_by_subscription(
        self,
        tenant_id: str,
        subscription_id: str,
        limit: int = 50,
        offset: int = 0,
    ) -> List[WebhookDeliveryRecord]:
        """List delivery history for a webhook subscription."""
        ...

    async def update_delivery_status(
        self,
        tenant_id: str,
        delivery_id: str,
        status: str,
        response_status_code: Optional[int] = None,
        error_message: Optional[str] = None,
        next_retry_at: Optional[datetime] = None,
    ) -> Optional[WebhookDeliveryRecord]:
        """Update status and outcome of an in-flight delivery attempt."""
        ...


@runtime_checkable
class AuditRepository(Protocol):
    """Repository port for structured audit event persistence (SEC-012)."""

    async def append_event(self, event: AuditEventRecord) -> None:
        """Append a structured, non-PII audit event."""
        ...

    async def query_events(
        self,
        tenant_id: str,
        resource_id: Optional[str] = None,
        action: Optional[str] = None,
        start_time: Optional[datetime] = None,
        end_time: Optional[datetime] = None,
        limit: int = 100,
        offset: int = 0,
    ) -> List[AuditEventRecord]:
        """Query historical audit events for compliance inspection."""
        ...
