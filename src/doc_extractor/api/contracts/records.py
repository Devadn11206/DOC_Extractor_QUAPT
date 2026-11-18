"""
Typed data contracts for repositories, queues, and domain entities (Module M5).

All records are strongly typed, immutable or strictly validated via Pydantic,
and enforce tenant scoping where appropriate.
"""

from datetime import datetime, timezone
import json
import math
import re
from typing import Any, Dict, List, Optional
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from doc_extractor.api.contracts.tenant_bound import TenantBoundModel

from doc_extractor.models import DocumentRecord as ModuleADocumentRecord
from doc_extractor.models import DocumentStatus


def utc_now() -> datetime:
    """Helper returning timezone-aware current UTC time."""
    return datetime.now(timezone.utc)


class DocumentResource(BaseModel):
    """
    API RESPONSE DTO for one document (API-002 / API-015). It is NOT a domain model and NOT persisted.

    The canonical, durable record of a document is Module A's ``doc_extractor.models.DocumentRecord`` (written at
    ingestion, with the SCH-012 status and the internal ingestion state). This DTO is mapped from it with
    ``from_record`` and deliberately omits ``tenant_id`` and ``internal_state`` (never exposed) and reduces the error to
    its code and message (API-011: no details). There is no second document model: add a field to Module A's record
    first (for example document type and schema binding, PRD section 20), then to this DTO.
    """
    model_config = ConfigDict(extra="forbid", frozen=True)

    document_id: str = Field(..., min_length=1)
    status: DocumentStatus = Field(..., description="SCH-012 status as Module A defines it (queued / rejected_malware: decision M5-D04)")
    filename: str = Field(..., min_length=1, description="The filename the client claimed, without path")
    mime_type: str
    file_size_bytes: int = Field(..., ge=0)
    page_count: int = Field(..., ge=0)
    sha256: str = Field(..., min_length=64, max_length=64)
    created_at: datetime
    updated_at: datetime
    error_code: Optional[str] = None
    error_message: Optional[str] = None

    @classmethod
    def from_record(cls, record: ModuleADocumentRecord) -> "DocumentResource":
        """Map Module A's canonical record to the response shape."""
        if not isinstance(record, ModuleADocumentRecord):
            raise TypeError("DocumentResource is mapped from Module A's doc_extractor.models.DocumentRecord")
        error = record.error or {}
        return cls(
            document_id=record.document_id,
            status=record.status,
            filename=record.claimed_filename,
            mime_type=record.mime_type,
            file_size_bytes=record.file_size_bytes,
            page_count=record.page_count,
            sha256=record.file_hash_sha256,
            created_at=record.created_at,
            updated_at=record.updated_at,
            error_code=error.get("code"),
            error_message=error.get("message"),
        )


class ReviewTaskRecord(TenantBoundModel):
    """
    Human-in-the-loop review task (FR-REV, API-005, API-016). The review domain's OWN entity: no authoritative model
    of a review task exists elsewhere (Module B's ``ReviewerOverrideRequest`` is a one-shot document-type override,
    not a task with an owner and a life cycle), so this is not a duplicate.

    PROVISIONAL: the status vocabulary below mixes task state with document outcome and the PRD does not yet define
    task states (decision M5-D11); it is expected to change before any adapter persists it.
    """
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    task_id: str = Field(..., min_length=1, description="Unique review task identifier")
    document_id: str = Field(..., min_length=1, description="Associated document ID")
    tenant_id: str = Field(..., min_length=1, description="Owning tenant identifier")
    status: str = Field(default="pending", description="Task status (pending, in_review, approved, rejected)")
    assigned_reviewer_id: Optional[str] = Field(None, description="Principal ID of assigned reviewer")
    priority: int = Field(default=0, description="Task processing priority")
    created_at: datetime = Field(default_factory=utc_now, description="Task creation timestamp")
    updated_at: datetime = Field(default_factory=utc_now, description="Last update timestamp")
    completed_at: Optional[datetime] = Field(None, description="Task completion timestamp")
    reviewer_notes: Optional[str] = Field(None, description="Reviewer notes or reason for rejection")
    metadata: Dict[str, Any] = Field(default_factory=dict, description="Redacted task context metadata")


class ApiKeyRecord(TenantBoundModel):
    """API key credential and authorization scope record with deterministic SHA-256 hash (SEC-002)."""
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    key_id: str = Field(..., min_length=1, description="Public API key identifier (prefix)")
    tenant_id: str = Field(..., min_length=1, description="Bound tenant identifier")
    name: str = Field(..., min_length=1, description="Descriptive label for the API key")
    key_hash: str = Field(..., min_length=64, max_length=64, description="Cryptographic SHA-256 hash of secret token")
    key_prefix: str = Field(..., min_length=4, max_length=16, description="Visible key prefix for identification")
    scopes: List[str] = Field(default_factory=list, description="Granted API scopes/permissions")
    roles: List[str] = Field(default_factory=list, description="Canonical roles assigned to the API key")
    created_at: datetime = Field(default_factory=utc_now, description="Key generation timestamp")
    expires_at: Optional[datetime] = Field(None, description="Optional key expiration timestamp")
    revoked_at: Optional[datetime] = Field(None, description="Timestamp when key was revoked, if any")
    is_active: bool = Field(default=True, description="Whether key is currently active")
    created_by: Optional[str] = Field(None, description="Principal ID that created this key")
    last_used_at: Optional[datetime] = Field(None, description="Timestamp when key was last used for authentication")


class ApiKeySummary(TenantBoundModel):
    """Safe, redacted summary of an API key excluding raw secrets and full hashes (SEC-002, SEC-012)."""
    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)

    key_id: str = Field(..., min_length=1, description="Public API key identifier")
    tenant_id: str = Field(..., min_length=1, description="Bound tenant identifier")
    name: str = Field(..., min_length=1, description="Descriptive label")
    key_prefix: str = Field(..., min_length=4, max_length=16, description="Visible key prefix for identification")
    scopes: List[str] = Field(default_factory=list, description="Granted API scopes")
    roles: List[str] = Field(default_factory=list, description="Assigned tenant roles")
    created_at: datetime = Field(..., description="Key generation timestamp")
    expires_at: Optional[datetime] = Field(None, description="Key expiration timestamp")
    revoked_at: Optional[datetime] = Field(None, description="Timestamp when revoked, if any")
    is_active: bool = Field(default=True, description="Active status")
    created_by: Optional[str] = Field(None, description="Principal ID that created this key")
    last_used_at: Optional[datetime] = Field(None, description="Timestamp when key was last used")

    @classmethod
    def from_record(cls, record: ApiKeyRecord) -> "ApiKeySummary":
        """Factory constructing a sanitized summary strictly omitting key_hash."""
        return cls(
            key_id=record.key_id,
            tenant_id=record.tenant_id,
            name=record.name,
            key_prefix=record.key_prefix,
            scopes=list(record.scopes),
            roles=list(record.roles),
            created_at=record.created_at,
            expires_at=record.expires_at,
            revoked_at=record.revoked_at,
            is_active=record.is_active,
            created_by=record.created_by,
            last_used_at=record.last_used_at,
        )


class ApiKeyCreationResult(TenantBoundModel):
    """Result of API key creation exposing the plaintext secret token strictly once."""
    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)

    key_id: str = Field(..., min_length=1, description="Public API key identifier")
    tenant_id: str = Field(..., min_length=1, description="Bound tenant identifier")
    name: str = Field(..., min_length=1, description="Descriptive label")
    key_prefix: str = Field(..., min_length=4, max_length=16, description="Visible key prefix")
    scopes: List[str] = Field(default_factory=list, description="Granted API scopes")
    roles: List[str] = Field(default_factory=list, description="Assigned tenant roles")
    created_at: datetime = Field(..., description="Key generation timestamp")
    expires_at: Optional[datetime] = Field(None, description="Key expiration timestamp")
    created_by: Optional[str] = Field(None, description="Principal ID that created this key")
    raw_secret: str = Field(..., min_length=32, description="Plaintext secret token (disclosed ONCE)")

    def __repr__(self) -> str:
        return f"<ApiKeyCreationResult key_id={self.key_id!r} prefix={self.key_prefix!r} raw_secret='[REDACTED]'>"

    def __str__(self) -> str:
        return f"<ApiKeyCreationResult key_id={self.key_id!r} prefix={self.key_prefix!r}>"


class ApiKeyCreateRequest(BaseModel):
    """Client request schema for POST /api/v1/api-keys."""
    model_config = ConfigDict(extra="forbid")

    name: str = Field(..., min_length=1, max_length=128, description="Descriptive label for the API key")
    scopes: List[str] = Field(default_factory=list, description="Requested API scopes (e.g. ['documents:read'])")
    roles: List[str] = Field(default_factory=list, description="Requested tenant roles (e.g. ['integrator'])")
    expires_at: Optional[datetime] = Field(None, description="Expiration timestamp (max 90 days, timezone-aware UTC)")
    expires_in_days: Optional[int] = Field(None, ge=1, le=90, description="Optional key lifetime in days (1-90)")

    @model_validator(mode="after")
    def validate_expiration_exclusivity(self) -> "ApiKeyCreateRequest":
        if self.expires_at is not None and self.expires_in_days is not None:
            raise ValueError("Provide either 'expires_at' or 'expires_in_days', not both.")
        return self


class ApiKeyRevokeResponse(BaseModel):
    """Response schema for DELETE /api/v1/api-keys/{id}."""
    model_config = ConfigDict(extra="forbid")

    key_id: str = Field(..., min_length=1, description="Revoked API key identifier")
    revoked: bool = Field(True, description="Whether key was successfully revoked")
    message: str = Field("API key revoked successfully.", description="Human-readable outcome message")


class WebhookSubscriptionRecord(TenantBoundModel):
    """Tenant webhook event subscription registration."""
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    subscription_id: str = Field(..., min_length=1, description="Unique webhook subscription identifier")
    tenant_id: str = Field(..., min_length=1, description="Owning tenant identifier")
    target_url: str = Field(..., min_length=1, description="Destination HTTPS webhook endpoint")
    event_types: List[str] = Field(..., min_length=1, description="List of subscribed event patterns")
    secret_hash: str = Field(..., description="Hash of webhook signing secret")
    is_active: bool = Field(default=True, description="Whether subscription is enabled")
    created_at: datetime = Field(default_factory=utc_now, description="Creation timestamp")
    updated_at: datetime = Field(default_factory=utc_now, description="Last update timestamp")
    metadata: Dict[str, Any] = Field(default_factory=dict, description="Custom delivery metadata")


class WebhookDeliveryRecord(TenantBoundModel):
    """Audit record of a single webhook dispatch attempt."""
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    delivery_id: str = Field(..., min_length=1, description="Unique dispatch attempt identifier")
    subscription_id: str = Field(..., min_length=1, description="Target subscription identifier")
    tenant_id: str = Field(..., min_length=1, description="Owning tenant identifier")
    event_id: str = Field(..., min_length=1, description="Triggering event identifier")
    event_type: str = Field(..., min_length=1, description="Type of event dispatched")
    attempt_number: int = Field(default=1, ge=1, description="Dispatch attempt sequence number")
    status: str = Field(default="pending", description="Delivery status (pending, delivered, failed, dead_letter)")
    response_status_code: Optional[int] = Field(None, description="HTTP response code returned by target")
    error_message: Optional[str] = Field(None, description="Error diagnostics if dispatch failed")
    created_at: datetime = Field(default_factory=utc_now, description="Dispatch trigger timestamp")
    completed_at: Optional[datetime] = Field(None, description="Dispatch resolution timestamp")
    next_retry_at: Optional[datetime] = Field(None, description="Scheduled retry timestamp if transient failure")


class AuditEventRecord(TenantBoundModel):
    """
    One platform audit event (SEC-008/SEC-012: identifiers and codes only). Distinct from M4's ``LifecycleEvent``,
    which records schema-lifecycle transitions inside the schema domain; both feed the audit store (owner:
    Team Member 3), neither replaces the other.
    """
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    event_id: str = Field(..., min_length=1, description="Unique audit event identifier")
    actor_id: str = Field(..., min_length=1, description="Principal or system actor identifier")
    tenant_id: str = Field(..., min_length=1, description="Scoping tenant identifier")
    action: str = Field(..., min_length=1, description="Canonical action type (e.g., document.upload)")
    resource_id: str = Field(..., min_length=1, description="Identifier of affected resource")
    resource_type: str = Field(..., min_length=1, description="Type of affected resource (e.g., document, schema)")
    timestamp: datetime = Field(default_factory=utc_now, description="UTC event occurrence timestamp")
    request_id: Optional[str] = Field(None, description="Correlated request ID")
    outcome: str = Field(default="SUCCESS", description="Event outcome status (e.g., SUCCESS, DENIED, FAILED)")
    metadata: Dict[str, Any] = Field(default_factory=dict, description="Redacted, non-PII audit context")


# Bounds of a job message's ``payload`` (identifiers and execution options only, never content): see DocumentProcessingMessage.
PAYLOAD_MAX_KEYS = 32
PAYLOAD_MAX_STRING_LENGTH = 256
PAYLOAD_MAX_LIST_ITEMS = 100
PAYLOAD_MAX_BYTES = 4096
_PAYLOAD_KEY = re.compile(r"[a-z][a-z0-9_]{0,63}")


def _payload_scalar_ok(value: Any) -> bool:
    if value is None or isinstance(value, (bool, int)):
        return True
    if isinstance(value, float):
        return math.isfinite(value)
    return isinstance(value, str) and len(value) <= PAYLOAD_MAX_STRING_LENGTH


class DocumentProcessingMessage(TenantBoundModel):
    """
    Durable JOB message: "process this document" travelling API -> broker -> worker (ARC-003). It carries identifiers
    and execution options only, never page images or extraction content, and it survives restarts and redelivery.

    It is NOT Module A's stage hand-off. ``doc_extractor.handoff.ModuleAHandoffPayload`` is the in-process result of
    Module A's ingestion, passed to Module B within one pipeline run (``QueueAdapter``, in-memory), and carries the
    page geometry. A job message refers to a document by id; the worker reads the persisted hand-off by that id.
    """
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    message_id: str = Field(..., min_length=1, description="Unique queue message identifier")
    document_id: str = Field(..., min_length=1, description="Target document identifier")
    tenant_id: str = Field(..., min_length=1, description="Owning tenant identifier")
    event_id: str = Field(..., min_length=1, description="Originating event identifier")
    deduplication_id: Optional[str] = Field(None, description="Idempotency deduplication key")
    attempt_count: int = Field(default=1, ge=1, description="Current processing attempt number")
    max_retries: int = Field(default=3, ge=0, description="Maximum allowed processing retries before dead letter")
    created_at: datetime = Field(default_factory=utc_now, description="Message enqueue timestamp")
    payload: Dict[str, Any] = Field(
        default_factory=dict,
        description=(
            "Execution options as identifiers and scalars only: snake_case keys; values are null, bool, int, finite float, a "
            f"string of at most {PAYLOAD_MAX_STRING_LENGTH} characters, or a flat list of at most {PAYLOAD_MAX_LIST_ITEMS} such "
            f"scalars; at most {PAYLOAD_MAX_KEYS} keys and {PAYLOAD_MAX_BYTES} bytes as JSON. No nested objects, bytes or content."
        ),
    )
    correlation_id: Optional[str] = Field(None, description="Correlation identifier for distributed tracing")

    @field_validator("payload")
    @classmethod
    def _payload_carries_options_not_content(cls, payload: Dict[str, Any]) -> Dict[str, Any]:
        """Bounded, flat and scalar: a job message refers to a document by id, it cannot carry page images or extracted text."""
        if len(payload) > PAYLOAD_MAX_KEYS:
            raise ValueError(f"payload has more than {PAYLOAD_MAX_KEYS} keys")
        for key, value in payload.items():
            if not isinstance(key, str) or not _PAYLOAD_KEY.fullmatch(key):
                raise ValueError("payload has a key that is not a snake_case identifier")
            if isinstance(value, list):
                if len(value) > PAYLOAD_MAX_LIST_ITEMS or not all(_payload_scalar_ok(item) for item in value):
                    raise ValueError("payload has a list that is too long or holds more than flat scalars")
            elif not _payload_scalar_ok(value):
                raise ValueError("payload has a value that is not a bounded scalar (nested objects, bytes and long text are refused)")
        if len(json.dumps(payload, separators=(",", ":"), allow_nan=False).encode("utf-8")) > PAYLOAD_MAX_BYTES:
            raise ValueError(f"payload is larger than {PAYLOAD_MAX_BYTES} bytes")
        return payload


class QueueEnvelope(BaseModel):
    """Delivery wrapper of a durable job message: lease and receipt handle for ack/nack (ARC-003). Broker-facing only."""
    model_config = ConfigDict(extra="forbid")

    receipt_handle: str = Field(..., min_length=1, description="Opaque lease/receipt handle for ack/nack")
    message: DocumentProcessingMessage = Field(..., description="Strongly typed processing message")
    approximate_receive_count: int = Field(default=1, ge=1, description="Number of times message was delivered")
    visible_until: datetime = Field(..., description="Timestamp until which message visibility is leased")
