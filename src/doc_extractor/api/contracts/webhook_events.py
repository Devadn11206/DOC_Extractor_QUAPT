"""
Webhook event contracts and wire format (PRD v1.5 section 7.1), Phase 4 / Webhook Dispatcher foundation. PROVISIONAL.

Four events exist (PRD 7.1): ``document.completed`` (status becomes ``auto_approved`` or ``reviewed``), ``document.needs_review``
(a review task is created), ``document.failed`` (a stage fails after retries) and ``schema.approved`` (a schema version is
approved). Each is a typed, frozen, validated model with a unique ``event_id`` (consumers de-duplicate by it: delivery is at
least once), an ``event_type`` and an ``occurred_at`` instant. This module only DEFINES and SERIALIZES events: it builds no
event from a live document, sends nothing, stores nothing and reads no clock (``occurred_at`` is always supplied by the caller).

THE PRD DEFINES NO PAYLOAD FIELDS. Everything below beyond the four event names and their trigger conditions is a provisional
design choice, kept to the minimum a consumer needs to act (it can fetch details through the API with its own credentials):

| event_type              | data                                                                               |
| ----------------------- | ---------------------------------------------------------------------------------- |
| ``document.completed``  | ``document_id``; ``status`` (``auto_approved`` or ``reviewed``, the PRD's trigger)  |
| ``document.needs_review`` | ``document_id``; ``task_id`` of the review task that was created                  |
| ``document.failed``     | ``document_id``; ``reason_code`` (a fixed upper-case token; GR-018 "a reason code") |
| ``schema.approved``     | ``schema_id``; ``schema_version`` (a positive integer)                             |

PRIVACY. No payload carries an extracted value, evidence text, document content, a filename, a prompt, a model or run
diagnostic, a free-text message or reason, a person's identifier (the approver and author of a schema are NOT included) or the
tenant (the receiving endpoint belongs to the tenant; the dispatcher selects subscriptions by tenant outside the payload). Every
string field is an identifier or a fixed token validated against a strict ASCII pattern, so a payload cannot be a vehicle for
content: ``extra`` fields are forbidden and nothing free-form is accepted.

WIRE FORMAT (provisional; no external implementation is assumed compatible): the request body is the UTF-8 (ASCII) bytes of a
single JSON object with exactly the keys ``event_id``, ``event_type``, ``occurred_at`` and ``data``, serialized CANONICALLY by
``serialize_event``: keys sorted, no whitespace (``separators=(",", ":")``), ``ensure_ascii``, no NaN, ``occurred_at`` as UTC
ISO 8601 with six fractional digits and a ``Z`` suffix (``2026-03-01T12:00:00.000000Z``). The signature (see
``services/webhook_signing.py``) is computed over exactly these bytes, so a consumer must verify the raw body BEFORE parsing it.
There is no version field yet: introducing one is a wire-format decision for the owner.
"""

import json
import re
import uuid
from datetime import datetime, timezone
from enum import Enum
from typing import Annotated, Any, Dict, Literal, Optional, Union

from pydantic import BaseModel, ConfigDict, Field, StrictInt, StrictStr, TypeAdapter, ValidationError, field_validator

from doc_extractor.api.contracts.documents import SAFE_DOCUMENT_ID_REGEX
from doc_extractor.api.contracts.review_tasks import SAFE_TASK_ID_REGEX

MAX_PAYLOAD_BYTES = 16 * 1024                 # defensive bound for ``parse_event`` (an event is a few hundred bytes), not a policy

_EVENT_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_\-]{7,127}")
_SCHEMA_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.\-]{0,63}")                 # same shape as correction validation
_REASON_CODE = re.compile(r"[A-Z][A-Z0-9_]{1,63}")                           # same shape as the other fixed codes
_WIRE_TIME = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}\.[0-9]{6}Z")
_MAX_SCHEMA_VERSION = 999_999_999


class WebhookEventType(str, Enum):
    DOCUMENT_COMPLETED = "document.completed"
    DOCUMENT_NEEDS_REVIEW = "document.needs_review"
    DOCUMENT_FAILED = "document.failed"
    SCHEMA_APPROVED = "schema.approved"


class WebhookEventError(Exception):
    """A refused event or payload. ``code`` is fixed and the message is fixed: no input is ever echoed."""

    INVALID_EVENT = "INVALID_EVENT"
    INVALID_PAYLOAD = "INVALID_PAYLOAD"
    _MESSAGES = {
        INVALID_EVENT: "The webhook event is not valid.",
        INVALID_PAYLOAD: "The webhook payload is not valid.",
    }

    def __init__(self, code: str) -> None:
        self.code = code if code in self._MESSAGES else self.INVALID_EVENT
        super().__init__(self._MESSAGES[self.code])


# ---- field validation --------------------------------------------------------------------------------------------------------

def _matches(pattern: "re.Pattern[str]", value: Any) -> bool:
    return isinstance(value, str) and pattern.fullmatch(value) is not None


def _canonical_time(value: Any) -> datetime:
    """A timezone-aware ``datetime`` (any offset, normalized to UTC) or the canonical wire string; nothing else."""
    try:
        if isinstance(value, datetime):
            if value.tzinfo is None or value.utcoffset() is None:
                raise ValueError("a timezone-aware time is required")
            return value.astimezone(timezone.utc)
        if _matches(_WIRE_TIME, value):
            return datetime.fromisoformat(value[:-1] + "+00:00").astimezone(timezone.utc)
    except OverflowError:                       # e.g. year 1 with a positive offset: it has no UTC representation. Pydantic turns
        raise ValueError("a time that UTC cannot represent") from None      # only ValueError into a validation error
    raise ValueError("not a timestamp")


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)


class DocumentCompletedData(_Strict):
    document_id: StrictStr
    status: Literal["auto_approved", "reviewed"]

    @field_validator("document_id")
    @classmethod
    def _document_id(cls, value: str) -> str:
        if not SAFE_DOCUMENT_ID_REGEX.fullmatch(value):
            raise ValueError("not a document identifier")
        return value


class DocumentNeedsReviewData(_Strict):
    document_id: StrictStr
    task_id: StrictStr

    @field_validator("document_id")
    @classmethod
    def _document_id(cls, value: str) -> str:
        if not SAFE_DOCUMENT_ID_REGEX.fullmatch(value):
            raise ValueError("not a document identifier")
        return value

    @field_validator("task_id")
    @classmethod
    def _task_id(cls, value: str) -> str:
        if not SAFE_TASK_ID_REGEX.fullmatch(value):
            raise ValueError("not a task identifier")
        return value


class DocumentFailedData(_Strict):
    document_id: StrictStr
    reason_code: StrictStr

    @field_validator("document_id")
    @classmethod
    def _document_id(cls, value: str) -> str:
        if not SAFE_DOCUMENT_ID_REGEX.fullmatch(value):
            raise ValueError("not a document identifier")
        return value

    @field_validator("reason_code")
    @classmethod
    def _reason_code(cls, value: str) -> str:
        if not _matches(_REASON_CODE, value):
            raise ValueError("not a fixed reason code")
        return value


class SchemaApprovedData(_Strict):
    schema_id: StrictStr
    schema_version: StrictInt = Field(..., ge=1, le=_MAX_SCHEMA_VERSION)

    @field_validator("schema_id")
    @classmethod
    def _schema_id(cls, value: str) -> str:
        if not _matches(_SCHEMA_ID, value):
            raise ValueError("not a schema identifier")
        return value


class _EventBase(_Strict):
    event_id: StrictStr
    occurred_at: datetime

    @field_validator("event_id")
    @classmethod
    def _event_id(cls, value: str) -> str:
        if not _matches(_EVENT_ID, value):
            raise ValueError("not an event identifier")
        return value

    @field_validator("occurred_at", mode="before")
    @classmethod
    def _occurred_at(cls, value: Any) -> datetime:
        return _canonical_time(value)


class DocumentCompletedEvent(_EventBase):
    event_type: Literal["document.completed"] = "document.completed"
    data: DocumentCompletedData


class DocumentNeedsReviewEvent(_EventBase):
    event_type: Literal["document.needs_review"] = "document.needs_review"
    data: DocumentNeedsReviewData


class DocumentFailedEvent(_EventBase):
    event_type: Literal["document.failed"] = "document.failed"
    data: DocumentFailedData


class SchemaApprovedEvent(_EventBase):
    event_type: Literal["schema.approved"] = "schema.approved"
    data: SchemaApprovedData


WebhookEvent = Annotated[
    Union[DocumentCompletedEvent, DocumentNeedsReviewEvent, DocumentFailedEvent, SchemaApprovedEvent],
    Field(discriminator="event_type"),
]
EVENT_CLASSES = (DocumentCompletedEvent, DocumentNeedsReviewEvent, DocumentFailedEvent, SchemaApprovedEvent)
_EVENT_ADAPTER: "TypeAdapter[Any]" = TypeAdapter(WebhookEvent)


# ---- construction ------------------------------------------------------------------------------------------------------------

def new_event_id() -> str:
    """
    A unique event identifier: ``evt_`` plus the 32 hex digits of a ``uuid4`` (the OS CSPRNG): 122 random bits, because six of
    its 128 bits are fixed (version and variant). It reads no clock.
    """
    return f"evt_{uuid.uuid4().hex}"


def _build(cls: Any, event_id: Optional[str], occurred_at: Any, **data: Any) -> Any:
    try:
        return cls(event_id=new_event_id() if event_id is None else event_id, occurred_at=occurred_at, data=data)
    except ValidationError:
        raise WebhookEventError(WebhookEventError.INVALID_EVENT) from None


def document_completed(
    *, document_id: str, status: str, occurred_at: datetime, event_id: Optional[str] = None
) -> DocumentCompletedEvent:
    """``occurred_at`` (timezone-aware) is required: there is no default and no clock. ``event_id`` defaults to a new unique one."""
    return _build(DocumentCompletedEvent, event_id, occurred_at, document_id=document_id, status=status)


def document_needs_review(
    *, document_id: str, task_id: str, occurred_at: datetime, event_id: Optional[str] = None
) -> DocumentNeedsReviewEvent:
    return _build(DocumentNeedsReviewEvent, event_id, occurred_at, document_id=document_id, task_id=task_id)


def document_failed(
    *, document_id: str, reason_code: str, occurred_at: datetime, event_id: Optional[str] = None
) -> DocumentFailedEvent:
    return _build(DocumentFailedEvent, event_id, occurred_at, document_id=document_id, reason_code=reason_code)


def schema_approved(
    *, schema_id: str, schema_version: int, occurred_at: datetime, event_id: Optional[str] = None
) -> SchemaApprovedEvent:
    return _build(SchemaApprovedEvent, event_id, occurred_at, schema_id=schema_id, schema_version=schema_version)


# ---- serialization -----------------------------------------------------------------------------------------------------------

def _wire(event: Any) -> Dict[str, Any]:
    return {
        "event_id": event.event_id,
        "event_type": event.event_type,
        "occurred_at": event.occurred_at.astimezone(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z"),
        "data": event.data.model_dump(mode="json"),
    }


def serialize_event(event: Any) -> bytes:
    """
    The canonical wire bytes of ``event`` (see the module docstring): the exact bytes that are signed and sent. Deterministic:
    identical events give identical bytes, whatever the order in which they were built or the offset of ``occurred_at``.
    """
    if not isinstance(event, EVENT_CLASSES):
        raise WebhookEventError(WebhookEventError.INVALID_EVENT)
    try:
        return json.dumps(_wire(event), sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False).encode("ascii")
    except (ValueError, TypeError, OverflowError):                     # OverflowError: an unvalidated event with an unrepresentable time
        raise WebhookEventError(WebhookEventError.INVALID_EVENT) from None


def _no_duplicate_keys(pairs: Any) -> Dict[str, Any]:
    result: Dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate key")
        result[key] = value
    return result


def _refuse_constant(_name: str) -> Any:
    raise ValueError("non-finite number")


def parse_event(payload: Union[bytes, str]) -> Any:
    """
    The event a wire payload carries, fully validated (strict: exact keys, canonical timestamp, fixed patterns). A duplicate key,
    NaN, an oversized payload or any deviation is a ``WebhookEventError``; nothing of the input is echoed. Meant for consumers and
    round-trip checks. A consumer verifies the signature over the raw bytes BEFORE calling this.
    """
    if isinstance(payload, str):
        try:
            payload = payload.encode("utf-8")
        except UnicodeEncodeError:
            raise WebhookEventError(WebhookEventError.INVALID_PAYLOAD) from None
    if not isinstance(payload, bytes) or not payload or len(payload) > MAX_PAYLOAD_BYTES:
        raise WebhookEventError(WebhookEventError.INVALID_PAYLOAD)
    try:
        document = json.loads(payload.decode("utf-8"), object_pairs_hook=_no_duplicate_keys, parse_constant=_refuse_constant)
        if not isinstance(document, dict):
            raise ValueError("not an object")
        return _EVENT_ADAPTER.validate_python(document)
    except (ValueError, TypeError, ValidationError, RecursionError):
        raise WebhookEventError(WebhookEventError.INVALID_PAYLOAD) from None
